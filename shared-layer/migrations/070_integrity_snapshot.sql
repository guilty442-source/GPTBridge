-- 070_integrity_snapshot.sql
-- Integrity Snapshot.
--
-- Per database release or backup, build:
--   snapshot_id, database_generation, schema_hash, audit_head_hash,
--   resource_merkle_root, migration_head, created_at
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- integrity_snapshot — point-in-time integrity snapshot
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.integrity_snapshot (
    snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    database_generation integer NOT NULL,
    schema_hash text NOT NULL,
    audit_head_hash text,
    resource_merkle_root text,
    migration_head integer NOT NULL,
    sqlite_digest_count integer DEFAULT 0,
    qdrant_integrity_count integer DEFAULT 0,
    reconcile_batch_count integer DEFAULT 0,
    release_id text REFERENCES gptbridge_index.database_release(release_id),
    backup_id text,
    created_at timestamptz NOT NULL DEFAULT now(),
    verified_at timestamptz,
    tamper_state text NOT NULL DEFAULT 'unverified' CHECK (tamper_state IN (
        'verified', 'unverified', 'mismatch', 'tampered',
        'incomplete', 'rebuild_required'
    ))
);

ALTER TABLE gptbridge_index.integrity_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.integrity_snapshot FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS integrity_snapshot_read ON gptbridge_index.integrity_snapshot;
CREATE POLICY integrity_snapshot_read ON gptbridge_index.integrity_snapshot
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS integrity_snapshot_write ON gptbridge_index.integrity_snapshot;
CREATE POLICY integrity_snapshot_write ON gptbridge_index.integrity_snapshot
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.integrity_snapshot FROM PUBLIC;
GRANT SELECT ON gptbridge_index.integrity_snapshot TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.integrity_snapshot
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS integrity_snapshot_gen_idx
    ON gptbridge_index.integrity_snapshot (database_generation, created_at);
CREATE INDEX IF NOT EXISTS integrity_snapshot_tamper_idx
    ON gptbridge_index.integrity_snapshot (tamper_state, created_at);

-- ============================================================================
-- create_integrity_snapshot() — create a new integrity snapshot
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.create_integrity_snapshot(
    p_database_generation integer,
    p_schema_hash text,
    p_migration_head integer,
    p_audit_head_hash text DEFAULT NULL,
    p_resource_merkle_root text DEFAULT NULL,
    p_release_id text DEFAULT NULL,
    p_backup_id text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_sqlite_count integer;
    v_qdrant_count integer;
    v_reconcile_count integer;
BEGIN
    SELECT count(*) INTO v_sqlite_count
    FROM gptbridge_index.sqlite_database_digest
    WHERE tamper_state = 'verified';

    SELECT count(*) INTO v_qdrant_count
    FROM gptbridge_index.qdrant_integrity_map
    WHERE integrity_state = 'verified';

    SELECT count(*) INTO v_reconcile_count
    FROM gptbridge_index.reconcile_batch_digest
    WHERE status = 'verified';

    INSERT INTO gptbridge_index.integrity_snapshot (
        database_generation, schema_hash, audit_head_hash,
        resource_merkle_root, migration_head,
        sqlite_digest_count, qdrant_integrity_count,
        reconcile_batch_count, release_id, backup_id
    )
    VALUES (
        p_database_generation, p_schema_hash, p_audit_head_hash,
        p_resource_merkle_root, p_migration_head,
        v_sqlite_count, v_qdrant_count,
        v_reconcile_count, p_release_id, p_backup_id
    )
    RETURNING snapshot_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_latest_snapshot() — get the latest integrity snapshot
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_latest_snapshot()
RETURNS TABLE (
    snapshot_id uuid,
    database_generation integer,
    schema_hash text,
    audit_head_hash text,
    resource_merkle_root text,
    migration_head integer,
    tamper_state text,
    created_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT snapshot_id, database_generation, schema_hash,
           audit_head_hash, resource_merkle_root, migration_head,
           tamper_state, created_at
    FROM gptbridge_index.integrity_snapshot
    ORDER BY created_at DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
