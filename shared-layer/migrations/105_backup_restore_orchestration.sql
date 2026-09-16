-- 103_backup_restore_orchestration.sql
-- Backup Restore Orchestration.
--
-- Disaster recovery flow:
--   select certified backup → verify hash → restore temporary instance →
--   schema verify → release verify → RLS verify → audit verify →
--   data integrity verify → promote → reconcile post-backup changes
--
-- NOT: restore file → production
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

CREATE TABLE IF NOT EXISTS gptbridge_index.backup_restore_orchestration (
    orchestration_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    backup_id text NOT NULL,
    backup_hash text,
    hash_verified boolean NOT NULL DEFAULT false,
    temp_instance_name text,
    status text NOT NULL DEFAULT 'selecting' CHECK (status IN (
        'selecting', 'verifying_hash', 'restoring_temp',
        'schema_verifying', 'release_verifying', 'rls_verifying',
        'audit_verifying', 'integrity_verifying',
        'promoting', 'reconciling_post_backup',
        'completed', 'failed'
    )),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    schema_verified boolean NOT NULL DEFAULT false,
    release_verified boolean NOT NULL DEFAULT false,
    rls_verified boolean NOT NULL DEFAULT false,
    audit_verified boolean NOT NULL DEFAULT false,
    integrity_verified boolean NOT NULL DEFAULT false,
    promoted boolean NOT NULL DEFAULT false,
    post_backup_reconciled boolean NOT NULL DEFAULT false,
    failure_reason text
);

ALTER TABLE gptbridge_index.backup_restore_orchestration ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.backup_restore_orchestration FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backup_restore_orch_read ON gptbridge_index.backup_restore_orchestration;
CREATE POLICY backup_restore_orch_read ON gptbridge_index.backup_restore_orchestration
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS backup_restore_orch_write ON gptbridge_index.backup_restore_orchestration;
CREATE POLICY backup_restore_orch_write ON gptbridge_index.backup_restore_orchestration
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.backup_restore_orchestration FROM PUBLIC;
GRANT SELECT ON gptbridge_index.backup_restore_orchestration TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.backup_restore_orchestration
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS backup_restore_status_idx
    ON gptbridge_index.backup_restore_orchestration (status, started_at);

CREATE OR REPLACE FUNCTION gptbridge_index.start_backup_restore(
    p_incident_id uuid,
    p_backup_id text,
    p_backup_hash text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.backup_restore_orchestration (
        incident_id, backup_id, backup_hash
    )
    VALUES (p_incident_id, p_backup_id, p_backup_hash)
    RETURNING orchestration_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.advance_backup_restore(
    p_orchestration_id uuid,
    p_new_status text,
    p_hash_verified boolean DEFAULT NULL,
    p_schema_verified boolean DEFAULT NULL,
    p_release_verified boolean DEFAULT NULL,
    p_rls_verified boolean DEFAULT NULL,
    p_audit_verified boolean DEFAULT NULL,
    p_integrity_verified boolean DEFAULT NULL,
    p_promoted boolean DEFAULT NULL,
    p_reconciled boolean DEFAULT NULL,
    p_failure_reason text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.backup_restore_orchestration
    SET status = p_new_status,
        hash_verified = COALESCE(p_hash_verified, hash_verified),
        schema_verified = COALESCE(p_schema_verified, schema_verified),
        release_verified = COALESCE(p_release_verified, release_verified),
        rls_verified = COALESCE(p_rls_verified, rls_verified),
        audit_verified = COALESCE(p_audit_verified, audit_verified),
        integrity_verified = COALESCE(p_integrity_verified, integrity_verified),
        promoted = COALESCE(p_promoted, promoted),
        post_backup_reconciled = COALESCE(p_reconciled, post_backup_reconciled),
        completed_at = CASE WHEN p_new_status IN ('completed', 'failed') THEN now() ELSE completed_at END,
        failure_reason = p_failure_reason
    WHERE orchestration_id = p_orchestration_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
