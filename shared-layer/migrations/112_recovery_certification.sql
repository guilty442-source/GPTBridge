-- 110_recovery_certification.sql
-- Recovery Certification.
--
-- Each Recovery Plan produces:
--   recovery_plan_id, plan_version, test_scenario, database_release,
--   starting_generation, ending_generation, RPO_result, RTO_result,
--   integrity_result, reconcile_result, audit_result, certification_status
-- Only CERTIFIED plans can enter production runtime.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A10/E10 — explicit-allowlist.

CREATE TABLE IF NOT EXISTS gptbridge_index.recovery_certification (
    certification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id text NOT NULL REFERENCES gptbridge_index.recovery_plan(plan_id),
    plan_version integer NOT NULL,
    test_scenario text,  -- chaos drill scenario code
    database_release_id text,
    starting_generation integer,
    ending_generation integer,
    rpo_result text CHECK (rpo_result IN ('met', 'exceeded', 'not_measured')),
    rto_result text CHECK (rto_result IN ('met', 'exceeded', 'not_measured')),
    rpo_seconds integer,
    rto_seconds integer,
    integrity_result text NOT NULL DEFAULT 'pending' CHECK (integrity_result IN (
        'pass', 'fail', 'pending'
    )),
    reconcile_result text NOT NULL DEFAULT 'pending' CHECK (reconcile_result IN (
        'pass', 'fail', 'pending'
    )),
    audit_result text NOT NULL DEFAULT 'pending' CHECK (audit_result IN (
        'pass', 'fail', 'pending'
    )),
    certification_status text NOT NULL DEFAULT 'pending' CHECK (certification_status IN (
        'pending', 'certified', 'rejected', 'expired'
    )),
    certified_at timestamptz,
    certified_by text,
    expires_at timestamptz,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.recovery_certification ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.recovery_certification FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS recovery_cert_read ON gptbridge_index.recovery_certification;
CREATE POLICY recovery_cert_read ON gptbridge_index.recovery_certification
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS recovery_cert_write ON gptbridge_index.recovery_certification;
CREATE POLICY recovery_cert_write ON gptbridge_index.recovery_certification
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.recovery_certification FROM PUBLIC;
GRANT SELECT ON gptbridge_index.recovery_certification TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.recovery_certification
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS recovery_cert_plan_idx
    ON gptbridge_index.recovery_certification (plan_id, certification_status);
CREATE INDEX IF NOT EXISTS recovery_cert_status_idx
    ON gptbridge_index.recovery_certification (certification_status, created_at);

CREATE OR REPLACE FUNCTION gptbridge_index.record_recovery_certification(
    p_plan_id text,
    p_plan_version integer,
    p_test_scenario text DEFAULT NULL,
    p_database_release_id text DEFAULT NULL,
    p_starting_generation integer DEFAULT NULL,
    p_ending_generation integer DEFAULT NULL,
    p_rpo_result text DEFAULT NULL,
    p_rto_result text DEFAULT NULL,
    p_rpo_seconds integer DEFAULT NULL,
    p_rto_seconds integer DEFAULT NULL,
    p_integrity_result text DEFAULT 'pending',
    p_reconcile_result text DEFAULT 'pending',
    p_audit_result text DEFAULT 'pending',
    p_notes text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.recovery_certification (
        plan_id, plan_version, test_scenario, database_release_id,
        starting_generation, ending_generation,
        rpo_result, rto_result, rpo_seconds, rto_seconds,
        integrity_result, reconcile_result, audit_result, notes
    )
    VALUES (
        p_plan_id, p_plan_version, p_test_scenario, p_database_release_id,
        p_starting_generation, p_ending_generation,
        p_rpo_result, p_rto_result, p_rpo_seconds, p_rto_seconds,
        p_integrity_result, p_reconcile_result, p_audit_result, p_notes
    )
    RETURNING certification_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.certify_recovery_plan_v2(
    p_certification_id uuid,
    p_certified_by text,
    p_expiry_days integer DEFAULT 365
) RETURNS void AS $$
DECLARE
    v_integrity text;
    v_reconcile text;
    v_audit text;
    v_plan_id text;
BEGIN
    SELECT integrity_result, reconcile_result, audit_result, plan_id
    INTO v_integrity, v_reconcile, v_audit, v_plan_id
    FROM gptbridge_index.recovery_certification
    WHERE certification_id = p_certification_id;

    IF v_integrity = 'pass' AND v_reconcile = 'pass' AND v_audit = 'pass' THEN
        UPDATE gptbridge_index.recovery_certification
        SET certification_status = 'certified',
            certified_at = now(),
            certified_by = p_certified_by,
            expires_at = now() + (p_expiry_days || ' days')::interval
        WHERE certification_id = p_certification_id;

        PERFORM gptbridge_index.certify_recovery_plan(v_plan_id, p_certified_by);
    ELSE
        UPDATE gptbridge_index.recovery_certification
        SET certification_status = 'rejected'
        WHERE certification_id = p_certification_id;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.is_recovery_plan_certified(
    p_plan_id text
) RETURNS boolean AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM gptbridge_index.recovery_certification
        WHERE plan_id = p_plan_id
          AND certification_status = 'certified'
          AND (expires_at IS NULL OR expires_at > now())
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
