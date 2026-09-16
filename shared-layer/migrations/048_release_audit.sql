-- 048_release_audit.sql
-- Database Release Audit.
--
-- Each release records:
--   release_id, previous_release, migration_set, executor, started_at,
--   completed_at, backup_id, certification_id, schema_hash, result,
--   failure_reason
--
-- This lets us answer: "which upgrade introduced the problem?"
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- database_release_audit — audit log for each release
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.database_release_audit (
    audit_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id text NOT NULL REFERENCES gptbridge_index.database_release(release_id),
    previous_release_id text,
    migration_set jsonb NOT NULL DEFAULT '[]'::jsonb,  -- list of migration IDs
    executor text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    backup_id text,
    certification_id uuid,  -- references rebuild_certification
    schema_hash text NOT NULL,
    result text NOT NULL DEFAULT 'in_progress' CHECK (result IN (
        'in_progress', 'succeeded', 'failed', 'rolled_back'
    )),
    failure_reason text,
    audited_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.database_release_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.database_release_audit FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS release_audit_read ON gptbridge_index.database_release_audit;
CREATE POLICY release_audit_read ON gptbridge_index.database_release_audit
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS release_audit_write ON gptbridge_index.database_release_audit;
CREATE POLICY release_audit_write ON gptbridge_index.database_release_audit
    FOR INSERT WITH CHECK (true);

REVOKE ALL ON gptbridge_index.database_release_audit FROM PUBLIC;
GRANT SELECT ON gptbridge_index.database_release_audit TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.database_release_audit
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS release_audit_release_idx
    ON gptbridge_index.database_release_audit (release_id, audited_at);
CREATE INDEX IF NOT EXISTS release_audit_result_idx
    ON gptbridge_index.database_release_audit (result, audited_at);

-- ============================================================================
-- start_release_audit() — start auditing a release
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.start_release_audit(
    p_release_id text,
    p_executor text,
    p_migration_set jsonb DEFAULT '[]'::jsonb,
    p_previous_release_id text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.database_release_audit (
        release_id, previous_release_id, migration_set, executor
    )
    VALUES (p_release_id, p_previous_release_id, p_migration_set, p_executor)
    RETURNING audit_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- complete_release_audit() — mark a release audit as succeeded or failed
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.complete_release_audit(
    p_audit_id uuid,
    p_succeeded boolean,
    p_schema_hash text,
    p_backup_id text DEFAULT NULL,
    p_certification_id uuid DEFAULT NULL,
    p_failure_reason text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.database_release_audit
    SET result = CASE WHEN p_succeeded THEN 'succeeded' ELSE 'failed' END,
        completed_at = now(),
        schema_hash = p_schema_hash,
        backup_id = p_backup_id,
        certification_id = p_certification_id,
        failure_reason = p_failure_reason
    WHERE audit_id = p_audit_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_release_audit_history() — get audit history for a release
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_release_audit_history(
    p_release_id text
) RETURNS TABLE (
    audit_id uuid,
    executor text,
    result text,
    started_at timestamptz,
    completed_at timestamptz,
    schema_hash text,
    failure_reason text
) AS $$
BEGIN
    RETURN QUERY
    SELECT audit_id, executor, result, started_at, completed_at,
           schema_hash, failure_reason
    FROM gptbridge_index.database_release_audit
    WHERE release_id = p_release_id
    ORDER BY audited_at DESC;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
