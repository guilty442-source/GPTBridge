-- 090_pg_recovery_verification.sql
-- PostgreSQL Recovery Verification.
--
-- PG service recovery ≠ DATABASE_READY. Verify in order:
--   connection → schema_version → database_release → roles → RLS →
--   audit → transport → integrity → generation
-- All pass → RECOVERABLE
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.pg_recovery_verification (
    verification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    verified_at timestamptz NOT NULL DEFAULT now(),
    verified_by text NOT NULL,
    connection_ok boolean NOT NULL DEFAULT false,
    schema_version_ok boolean NOT NULL DEFAULT false,
    database_release_ok boolean NOT NULL DEFAULT false,
    roles_ok boolean NOT NULL DEFAULT false,
    rls_ok boolean NOT NULL DEFAULT false,
    audit_ok boolean NOT NULL DEFAULT false,
    transport_ok boolean NOT NULL DEFAULT false,
    integrity_ok boolean NOT NULL DEFAULT false,
    generation_ok boolean NOT NULL DEFAULT false,
    overall_recoverable boolean NOT NULL DEFAULT false,
    failure_reason text
);

ALTER TABLE gptbridge_index.pg_recovery_verification ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.pg_recovery_verification FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS pg_recovery_verify_read ON gptbridge_index.pg_recovery_verification;
CREATE POLICY pg_recovery_verify_read ON gptbridge_index.pg_recovery_verification
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS pg_recovery_verify_write ON gptbridge_index.pg_recovery_verification;
CREATE POLICY pg_recovery_verify_write ON gptbridge_index.pg_recovery_verification
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.pg_recovery_verification FROM PUBLIC;
GRANT SELECT ON gptbridge_index.pg_recovery_verification TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.pg_recovery_verification
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS pg_recovery_verify_incident_idx
    ON gptbridge_index.pg_recovery_verification (incident_id, verified_at);

CREATE OR REPLACE FUNCTION gptbridge_index.record_pg_recovery_verification(
    p_incident_id uuid,
    p_verified_by text,
    p_connection_ok boolean,
    p_schema_version_ok boolean,
    p_database_release_ok boolean,
    p_roles_ok boolean,
    p_rls_ok boolean,
    p_audit_ok boolean,
    p_transport_ok boolean,
    p_integrity_ok boolean,
    p_generation_ok boolean,
    p_failure_reason text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_recoverable boolean;
BEGIN
    v_recoverable := p_connection_ok AND p_schema_version_ok AND
                     p_database_release_ok AND p_roles_ok AND p_rls_ok AND
                     p_audit_ok AND p_transport_ok AND p_integrity_ok AND
                     p_generation_ok;

    INSERT INTO gptbridge_index.pg_recovery_verification (
        incident_id, verified_by,
        connection_ok, schema_version_ok, database_release_ok,
        roles_ok, rls_ok, audit_ok, transport_ok,
        integrity_ok, generation_ok,
        overall_recoverable, failure_reason
    )
    VALUES (
        p_incident_id, p_verified_by,
        p_connection_ok, p_schema_version_ok, p_database_release_ok,
        p_roles_ok, p_rls_ok, p_audit_ok, p_transport_ok,
        p_integrity_ok, p_generation_ok,
        v_recoverable, p_failure_reason
    )
    RETURNING verification_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.is_pg_recoverable(
    p_incident_id uuid
) RETURNS boolean AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM gptbridge_index.pg_recovery_verification
        WHERE incident_id = p_incident_id AND overall_recoverable = true
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
