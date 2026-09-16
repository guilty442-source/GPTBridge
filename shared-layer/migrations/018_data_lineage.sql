-- 018_data_lineage.sql
-- Data Lineage: every central resource record carries its source module,
-- source revision, production method, sync path, and last writer, so the
-- full SQLite → PostgreSQL → Qdrant chain can be reverse-traced.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data; FORBID: sqlite-as-central.
--   A46/E22 — Audit: mandatory-ledger; write=governed-executor.
--   A10/E10 — Authorization: explicit-allowlist; deny-by-default.
--
-- This migration adds:
--   * gptbridge_index.data_lineage — one row per resource_id, recording
--     where the data came from, how it was produced, which sync path it
--     travelled, and who last wrote it.
--   * A trigger that auto-populates lineage on resource INSERT/UPDATE
--     using session variables (gptbridge.*) set by the runtime provenance
--     helper (migration 020 + runtime A5).
--   * gptbridge_index.resource_lineage — a single-query view joining
--     resource + data_lineage + registry.locations + rag.index_state so
--     the entire cross-engine chain is visible in one row.

-- ============================================================================
-- data_lineage table
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.data_lineage (
    resource_id text PRIMARY KEY
        REFERENCES gptbridge_index.resource(resource_id) ON DELETE CASCADE,
    source_module text NOT NULL,
    source_revision bigint NOT NULL DEFAULT 1 CHECK (source_revision >= 1),
    produce_method text NOT NULL DEFAULT 'direct-write' CHECK (
        produce_method IN (
            'direct-write',
            'reconcile-push',
            'reconcile-pull',
            'migration-import',
            'rebuild',
            'derived',
            'restore'
        )
    ),
    sync_path text NOT NULL DEFAULT 'sqlite→postgresql' CHECK (
        sync_path IN (
            'sqlite→postgresql',
            'postgresql→qdrant',
            'sqlite→postgresql→qdrant',
            'postgresql-only',
            'qdrant-only',
            'restore'
        )
    ),
    last_writer_id text NOT NULL DEFAULT 'unknown',
    last_writer_at timestamptz NOT NULL DEFAULT now(),
    last_writer_actor_id text,
    last_writer_executor_id text,
    last_writer_decision_id text,
    last_writer_correlation_id text,
    lineage_metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE gptbridge_index.data_lineage ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.data_lineage FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS data_lineage_read ON gptbridge_index.data_lineage;
CREATE POLICY data_lineage_read ON gptbridge_index.data_lineage
    FOR SELECT USING (gptbridge_security.can_read(
        (SELECT module_id FROM gptbridge_index.resource
         WHERE resource_id = data_lineage.resource_id)
    ));

DROP POLICY IF EXISTS data_lineage_write ON gptbridge_index.data_lineage;
CREATE POLICY data_lineage_write ON gptbridge_index.data_lineage
    FOR ALL
    USING (gptbridge_security.can_write(
        (SELECT module_id FROM gptbridge_index.resource
         WHERE resource_id = data_lineage.resource_id)
    ))
    WITH CHECK (gptbridge_security.can_write(
        (SELECT module_id FROM gptbridge_index.resource
         WHERE resource_id = data_lineage.resource_id)
    ));

REVOKE ALL ON gptbridge_index.data_lineage FROM PUBLIC;
GRANT SELECT ON gptbridge_index.data_lineage TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.data_lineage
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS data_lineage_source_module_idx
    ON gptbridge_index.data_lineage (source_module, source_revision);
CREATE INDEX IF NOT EXISTS data_lineage_writer_idx
    ON gptbridge_index.data_lineage (last_writer_id, last_writer_at);

-- ============================================================================
-- Auto-populate lineage on resource INSERT/UPDATE.
-- Reads session variables set by the runtime provenance helper:
--   gptbridge.actor_id, gptbridge.executor_id, gptbridge.decision_id,
--   gptbridge.correlation_id, gptbridge.source_revision
-- Falls back to 'unknown' / defaults when variables are not set, so the
-- trigger is safe for migrations and manual repairs.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.auto_populate_lineage()
RETURNS trigger AS $$
DECLARE
    v_actor text;
    v_executor text;
    v_decision text;
    v_correlation text;
    v_source_rev bigint;
    v_source_module text;
BEGIN
    v_actor := COALESCE(
        nullif(current_setting('gptbridge.actor_id', true), ''),
        'unknown'
    );
    v_executor := COALESCE(
        nullif(current_setting('gptbridge.executor_id', true), ''),
        'unknown'
    );
    v_decision := nullif(current_setting('gptbridge.decision_id', true), '');
    v_correlation := nullif(current_setting('gptbridge.correlation_id', true), '');
    v_source_rev := COALESCE(
        nullif(current_setting('gptbridge.source_revision', true), '')::bigint,
        NEW.version
    );
    v_source_module := NEW.module_id;

    INSERT INTO gptbridge_index.data_lineage (
        resource_id, source_module, source_revision, produce_method,
        sync_path, last_writer_id, last_writer_at,
        last_writer_actor_id, last_writer_executor_id,
        last_writer_decision_id, last_writer_correlation_id
    )
    VALUES (
        NEW.resource_id, v_source_module, v_source_rev, 'direct-write',
        'sqlite→postgresql', v_executor, now(),
        v_actor, v_executor, v_decision, v_correlation
    )
    ON CONFLICT (resource_id) DO UPDATE SET
        source_module = EXCLUDED.source_module,
        source_revision = EXCLUDED.source_revision,
        last_writer_id = EXCLUDED.last_writer_id,
        last_writer_at = EXCLUDED.last_writer_at,
        last_writer_actor_id = EXCLUDED.last_writer_actor_id,
        last_writer_executor_id = EXCLUDED.last_writer_executor_id,
        last_writer_decision_id = EXCLUDED.last_writer_decision_id,
        last_writer_correlation_id = EXCLUDED.last_writer_correlation_id;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS resource_lineage_populate ON gptbridge_index.resource;
CREATE TRIGGER resource_lineage_populate
    AFTER INSERT OR UPDATE ON gptbridge_index.resource
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_index.auto_populate_lineage();

-- ============================================================================
-- resource_lineage view — single-query cross-engine chain.
-- Joins resource + data_lineage + registry.locations + rag.index_state so
-- a reverse trace from any resource_id reveals the full chain in one row.
-- ============================================================================
CREATE OR REPLACE VIEW gptbridge_index.resource_lineage AS
SELECT
    r.resource_id,
    r.module_id,
    r.owner_id,
    r.resource_type,
    r.resource_label,
    r.classification,
    r.version AS pg_revision,
    r.content_hash AS pg_hash,
    r.backend_generation,
    r.stale AS pg_stale,
    dl.source_module,
    dl.source_revision,
    dl.produce_method,
    dl.sync_path,
    dl.last_writer_id,
    dl.last_writer_at,
    dl.last_writer_actor_id,
    dl.last_writer_executor_id,
    dl.last_writer_decision_id,
    dl.last_writer_correlation_id,
    dl.lineage_metadata,
    loc.locator_id,
    loc.executor_type AS locator_executor_type,
    loc.location_key AS locator_location_key,
    loc.status AS locator_status,
    loc.physical_location IS NOT NULL AS has_physical_location,
    COALESCE(s.source_revision, 0) AS qdrant_revision,
    COALESCE(s.content_hash, '') AS qdrant_hash,
    COALESCE(s.status, 'missing') AS qdrant_index_status,
    CASE
        WHEN s.resource_id IS NULL THEN 'missing-qdrant'
        WHEN s.source_revision < r.version THEN 'qdrant-behind'
        WHEN s.source_revision > r.version THEN 'pg-behind'
        WHEN s.content_hash != COALESCE(r.content_hash, '') THEN 'hash-mismatch'
        ELSE 'in-sync'
    END AS qdrant_consistency
FROM gptbridge_index.resource r
LEFT JOIN gptbridge_index.data_lineage dl
    ON dl.resource_id = r.resource_id
LEFT JOIN registry.locations loc
    ON loc.resource_id = r.resource_id
LEFT JOIN gptbridge_rag.index_state s
    ON s.resource_id = r.resource_id;

GRANT SELECT ON gptbridge_index.resource_lineage TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_index.resource_lineage TO gptbridge_xingcheng_reader;
