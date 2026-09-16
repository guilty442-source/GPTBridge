-- 078_pg_major_upgrade_rehearsal.sql
-- PostgreSQL Major Upgrade Rehearsal.
--
-- Don't upgrade production directly. Flow:
--   backup → temporary clone → upgrade → migration check → RLS check →
--   transport test → reconcile test → performance baseline → certification
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- pg_major_upgrade_rehearsal — records major upgrade rehearsal steps
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.pg_major_upgrade_rehearsal (
    rehearsal_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    from_version text NOT NULL,
    to_version text NOT NULL,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'backup', 'cloning', 'upgrading', 'migration_check',
        'rls_check', 'transport_test', 'reconcile_test',
        'performance_baseline', 'certifying', 'succeeded', 'failed'
    )),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    backup_id text,
    clone_database text,
    migration_check_passed boolean,
    rls_check_passed boolean,
    transport_test_passed boolean,
    reconcile_test_passed boolean,
    performance_baseline_id uuid,
    certification_id uuid,
    failure_reason text,
    rehearsal_log jsonb DEFAULT '[]'::jsonb
);

ALTER TABLE gptbridge_index.pg_major_upgrade_rehearsal ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.pg_major_upgrade_rehearsal FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS pg_rehearsal_read ON gptbridge_index.pg_major_upgrade_rehearsal;
CREATE POLICY pg_rehearsal_read ON gptbridge_index.pg_major_upgrade_rehearsal
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS pg_rehearsal_write ON gptbridge_index.pg_major_upgrade_rehearsal;
CREATE POLICY pg_rehearsal_write ON gptbridge_index.pg_major_upgrade_rehearsal
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.pg_major_upgrade_rehearsal FROM PUBLIC;
GRANT SELECT ON gptbridge_index.pg_major_upgrade_rehearsal TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.pg_major_upgrade_rehearsal
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS pg_rehearsal_status_idx
    ON gptbridge_index.pg_major_upgrade_rehearsal (status, started_at);
CREATE INDEX IF NOT EXISTS pg_rehearsal_version_idx
    ON gptbridge_index.pg_major_upgrade_rehearsal (from_version, to_version);

-- ============================================================================
-- start_pg_rehearsal() — start a major upgrade rehearsal
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.start_pg_rehearsal(
    p_from_version text,
    p_to_version text,
    p_backup_id text
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.pg_major_upgrade_rehearsal (
        from_version, to_version, status, backup_id
    )
    VALUES (p_from_version, p_to_version, 'backup', p_backup_id)
    RETURNING rehearsal_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- advance_pg_rehearsal() — advance to next step
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.advance_pg_rehearsal(
    p_rehearsal_id uuid,
    p_new_status text,
    p_check_passed boolean DEFAULT NULL,
    p_failure_reason text DEFAULT NULL
) RETURNS void AS $$
DECLARE
    v_log jsonb;
BEGIN
    SELECT rehearsal_log INTO v_log
    FROM gptbridge_index.pg_major_upgrade_rehearsal
    WHERE rehearsal_id = p_rehearsal_id;

    v_log := v_log || jsonb_build_object(
        'step', p_new_status,
        'at', now()::text,
        'passed', p_check_passed,
        'reason', p_failure_reason
    );

    UPDATE gptbridge_index.pg_major_upgrade_rehearsal
    SET status = p_new_status,
        rehearsal_log = v_log,
        completed_at = CASE WHEN p_new_status IN ('succeeded', 'failed') THEN now()
                           ELSE completed_at END,
        migration_check_passed = CASE WHEN p_new_status = 'migration_check' THEN p_check_passed
                                       ELSE migration_check_passed END,
        rls_check_passed = CASE WHEN p_new_status = 'rls_check' THEN p_check_passed
                                ELSE rls_check_passed END,
        transport_test_passed = CASE WHEN p_new_status = 'transport_test' THEN p_check_passed
                                     ELSE transport_test_passed END,
        reconcile_test_passed = CASE WHEN p_new_status = 'reconcile_test' THEN p_check_passed
                                     ELSE reconcile_test_passed END,
        failure_reason = p_failure_reason
    WHERE rehearsal_id = p_rehearsal_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_rehearsal_summary() — get summary of a rehearsal
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_rehearsal_summary(
    p_rehearsal_id uuid
) RETURNS TABLE (
    from_version text,
    to_version text,
    status text,
    migration_check_passed boolean,
    rls_check_passed boolean,
    transport_test_passed boolean,
    reconcile_test_passed boolean
) AS $$
BEGIN
    RETURN QUERY
    SELECT from_version, to_version, status,
           migration_check_passed, rls_check_passed,
           transport_test_passed, reconcile_test_passed
    FROM gptbridge_index.pg_major_upgrade_rehearsal
    WHERE rehearsal_id = p_rehearsal_id;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
