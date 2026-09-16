-- 117_startup_phase_gate.sql
-- Startup Phase Gate.
--
-- Each phase has readiness gates. A phase is only complete when all its
-- gates pass. Gates check: governance ready, security ready, authority ready,
-- audit ready, manifest ready.
--
-- Audit must be writable BEFORE business writes enabled.
-- Gate: authority_ready AND audit_ready AND security_ready → write enabled.
--
-- Codex basis:
--   A10/E10 — explicit-allowlist.
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.startup_phase_gate (
    gate_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phase_number integer NOT NULL REFERENCES gptbridge_index.startup_phase(phase_number),
    gate_name text NOT NULL,
    gate_type text NOT NULL CHECK (gate_type IN (
        'governance_ready', 'security_ready', 'authority_ready',
        'audit_ready', 'manifest_ready', 'schema_ready',
        'integrity_ready', 'generation_ready', 'connection_ready',
        'collection_ready', 'transport_ready', 'reconcile_ready',
        'cache_valid', 'custom'
    )),
    check_expression text,  -- SQL expression or function name to evaluate
    required_for_write boolean NOT NULL DEFAULT false,
    required_for_requests boolean NOT NULL DEFAULT false,
    passed boolean NOT NULL DEFAULT false,
    checked_at timestamptz,
    failure_reason text,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.startup_phase_gate ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.startup_phase_gate FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS startup_gate_read ON gptbridge_index.startup_phase_gate;
CREATE POLICY startup_gate_read ON gptbridge_index.startup_phase_gate
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS startup_gate_write ON gptbridge_index.startup_phase_gate;
CREATE POLICY startup_gate_write ON gptbridge_index.startup_phase_gate
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.startup_phase_gate FROM PUBLIC;
GRANT SELECT ON gptbridge_index.startup_phase_gate TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.startup_phase_gate
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS startup_gate_phase_idx
    ON gptbridge_index.startup_phase_gate (phase_number, gate_name);
CREATE INDEX IF NOT EXISTS startup_gate_write_idx
    ON gptbridge_index.startup_phase_gate (phase_number)
    WHERE required_for_write = true;

CREATE OR REPLACE FUNCTION gptbridge_index.register_startup_gate(
    p_phase_number integer,
    p_gate_name text,
    p_gate_type text,
    p_check_expression text DEFAULT NULL,
    p_required_for_write boolean DEFAULT false,
    p_required_for_requests boolean DEFAULT false
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.startup_phase_gate (
        phase_number, gate_name, gate_type, check_expression,
        required_for_write, required_for_requests
    )
    VALUES (
        p_phase_number, p_gate_name, p_gate_type, p_check_expression,
        p_required_for_write, p_required_for_requests
    )
    RETURNING gate_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.set_gate_result(
    p_gate_id uuid,
    p_passed boolean,
    p_failure_reason text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.startup_phase_gate
    SET passed = p_passed, checked_at = now(), failure_reason = p_failure_reason
    WHERE gate_id = p_gate_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.is_phase_complete(
    p_phase_number integer
) RETURNS boolean AS $$
BEGIN
    RETURN NOT EXISTS (
        SELECT 1 FROM gptbridge_index.startup_phase_gate
        WHERE phase_number = p_phase_number AND passed = false
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.can_enable_write(
    p_phase_number integer
) RETURNS boolean AS $$
BEGIN
    -- Write is enabled only when all write-required gates pass
    RETURN NOT EXISTS (
        SELECT 1 FROM gptbridge_index.startup_phase_gate
        WHERE phase_number = p_phase_number
          AND required_for_write = true
          AND passed = false
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
