-- 067_sqlite_database_digest.sql
-- SQLite Database Digest.
--
-- Don't hash the entire DB every time.  Per logical data domain:
--   schema hash, revision head, row count, critical-table digest, generation
--
-- Codex basis:
--   A8/E21  — SQLite: owner-private-operational-state.
--   A44/E30 — four-functions-local.

-- ============================================================================
-- sqlite_database_digest — per-database digest
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.sqlite_database_digest (
    digest_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id text NOT NULL,
    database_path text NOT NULL,
    schema_hash text NOT NULL,
    revision_head integer NOT NULL DEFAULT 0,
    row_count bigint NOT NULL DEFAULT 0,
    critical_table_digest text,  -- hash of critical tables
    generation integer NOT NULL DEFAULT 0,
    computed_at timestamptz NOT NULL DEFAULT now(),
    verified_at timestamptz,
    tamper_state text NOT NULL DEFAULT 'unverified' CHECK (tamper_state IN (
        'verified', 'unverified', 'mismatch', 'tampered',
        'incomplete', 'rebuild_required'
    ))
);

ALTER TABLE gptbridge_index.sqlite_database_digest ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.sqlite_database_digest FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sqlite_digest_read ON gptbridge_index.sqlite_database_digest;
CREATE POLICY sqlite_digest_read ON gptbridge_index.sqlite_database_digest
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS sqlite_digest_write ON gptbridge_index.sqlite_database_digest;
CREATE POLICY sqlite_digest_write ON gptbridge_index.sqlite_database_digest
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.sqlite_database_digest FROM PUBLIC;
GRANT SELECT ON gptbridge_index.sqlite_database_digest TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.sqlite_database_digest
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS sqlite_digest_module_idx
    ON gptbridge_index.sqlite_database_digest (module_id, computed_at);
CREATE INDEX IF NOT EXISTS sqlite_digest_tamper_idx
    ON gptbridge_index.sqlite_database_digest (tamper_state, computed_at);

-- ============================================================================
-- record_sqlite_digest() — record a SQLite database digest
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_sqlite_digest(
    p_module_id text,
    p_database_path text,
    p_schema_hash text,
    p_revision_head integer,
    p_row_count bigint,
    p_critical_table_digest text DEFAULT NULL,
    p_generation integer DEFAULT 0
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.sqlite_database_digest (
        module_id, database_path, schema_hash, revision_head,
        row_count, critical_table_digest, generation
    )
    VALUES (
        p_module_id, p_database_path, p_schema_hash, p_revision_head,
        p_row_count, p_critical_table_digest, p_generation
    )
    RETURNING digest_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- verify_sqlite_digest() — verify a SQLite digest matches expected
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.verify_sqlite_digest(
    p_digest_id uuid,
    p_expected_schema_hash text,
    p_expected_row_count bigint
) RETURNS boolean AS $$
DECLARE
    v_actual_schema text;
    v_actual_rows bigint;
BEGIN
    SELECT schema_hash, row_count INTO v_actual_schema, v_actual_rows
    FROM gptbridge_index.sqlite_database_digest
    WHERE digest_id = p_digest_id;

    IF v_actual_schema = p_expected_schema_hash
       AND v_actual_rows = p_expected_row_count THEN
        UPDATE gptbridge_index.sqlite_database_digest
        SET tamper_state = 'verified', verified_at = now()
        WHERE digest_id = p_digest_id;
        RETURN true;
    ELSE
        UPDATE gptbridge_index.sqlite_database_digest
        SET tamper_state = 'mismatch'
        WHERE digest_id = p_digest_id;
        RETURN false;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_tampered_sqlite_dbs() — find SQLite DBs with tamper issues
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_tampered_sqlite_dbs(
    p_limit integer DEFAULT 100
) RETURNS TABLE (
    module_id text,
    database_path text,
    tamper_state text,
    computed_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT module_id, database_path, tamper_state, computed_at
    FROM gptbridge_index.sqlite_database_digest
    WHERE tamper_state IN ('mismatch', 'tampered', 'incomplete', 'rebuild_required')
    ORDER BY computed_at DESC
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
