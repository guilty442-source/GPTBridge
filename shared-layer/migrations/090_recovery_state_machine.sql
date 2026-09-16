-- 088_recovery_state_machine.sql
-- Recovery State Machine.
--
-- HEALTHY → SUSPECTED → INCIDENT_CONFIRMED → CONTAINED → DEGRADED →
-- RECOVERING → RECONCILING → VERIFYING → HEALTHY
-- Failure paths: QUARANTINED, RESTORE_REQUIRED, MANUAL_INTERVENTION
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A10/E10 — explicit-allowlist.

CREATE TABLE IF NOT EXISTS gptbridge_index.recovery_state_machine (
    state_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    from_state text NOT NULL CHECK (from_state IN (
        'HEALTHY', 'SUSPECTED', 'INCIDENT_CONFIRMED', 'CONTAINED',
        'DEGRADED', 'RECOVERING', 'RECONCILING', 'VERIFYING',
        'QUARANTINED', 'RESTORE_REQUIRED', 'MANUAL_INTERVENTION'
    )),
    to_state text NOT NULL CHECK (to_state IN (
        'HEALTHY', 'SUSPECTED', 'INCIDENT_CONFIRMED', 'CONTAINED',
        'DEGRADED', 'RECOVERING', 'RECONCILING', 'VERIFYING',
        'QUARANTINED', 'RESTORE_REQUIRED', 'MANUAL_INTERVENTION'
    )),
    transition_at timestamptz NOT NULL DEFAULT now(),
    transitioned_by text NOT NULL,
    reason text
);

ALTER TABLE gptbridge_index.recovery_state_machine ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.recovery_state_machine FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS recovery_state_read ON gptbridge_index.recovery_state_machine;
CREATE POLICY recovery_state_read ON gptbridge_index.recovery_state_machine
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS recovery_state_write ON gptbridge_index.recovery_state_machine;
CREATE POLICY recovery_state_write ON gptbridge_index.recovery_state_machine
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.recovery_state_machine FROM PUBLIC;
GRANT SELECT ON gptbridge_index.recovery_state_machine TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_index.recovery_state_machine
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS recovery_state_incident_idx
    ON gptbridge_index.recovery_state_machine (incident_id, transition_at);

-- Allowed transitions map
CREATE TABLE IF NOT EXISTS gptbridge_index.recovery_state_transition (
    from_state text NOT NULL,
    to_state text NOT NULL,
    allowed boolean NOT NULL DEFAULT true,
    requires_authority text,
    PRIMARY KEY (from_state, to_state)
);

ALTER TABLE gptbridge_index.recovery_state_transition ENABLE ROW LEVEL SECURITY;

INSERT INTO gptbridge_index.recovery_state_transition (from_state, to_state) VALUES
    ('HEALTHY', 'SUSPECTED'),
    ('SUSPECTED', 'INCIDENT_CONFIRMED'),
    ('SUSPECTED', 'HEALTHY'),
    ('INCIDENT_CONFIRMED', 'CONTAINED'),
    ('CONTAINED', 'DEGRADED'),
    ('DEGRADED', 'RECOVERING'),
    ('RECOVERING', 'RECONCILING'),
    ('RECONCILING', 'VERIFYING'),
    ('VERIFYING', 'HEALTHY'),
    ('INCIDENT_CONFIRMED', 'QUARANTINED'),
    ('CONTAINED', 'QUARANTINED'),
    ('DEGRADED', 'QUARANTINED'),
    ('QUARANTINED', 'RESTORE_REQUIRED'),
    ('RESTORE_REQUIRED', 'MANUAL_INTERVENTION'),
    ('RECOVERING', 'DEGRADED'),
    ('RECONCILING', 'DEGRADED'),
    ('VERIFYING', 'DEGRADED'),
    ('DEGRADED', 'MANUAL_INTERVENTION'),
    ('QUARANTINED', 'MANUAL_INTERVENTION')
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION gptbridge_index.transition_recovery_state(
    p_incident_id uuid,
    p_to_state text,
    p_transitioned_by text,
    p_reason text DEFAULT NULL
) RETURNS boolean AS $$
DECLARE
    v_current text;
    v_allowed boolean;
BEGIN
    SELECT status INTO v_current
    FROM gptbridge_index.recovery_incident
    WHERE incident_id = p_incident_id;

    IF v_current IS NULL THEN
        RETURN false;
    END IF;

    SELECT allowed INTO v_allowed
    FROM gptbridge_index.recovery_state_transition
    WHERE from_state = v_current AND to_state = p_to_state;

    IF NOT COALESCE(v_allowed, false) THEN
        RETURN false;
    END IF;

    INSERT INTO gptbridge_index.recovery_state_machine (
        incident_id, from_state, to_state, transitioned_by, reason
    )
    VALUES (p_incident_id, v_current, p_to_state, p_transitioned_by, p_reason);

    PERFORM gptbridge_index.advance_incident_status(
        p_incident_id, lower(p_to_state), p_reason
    );

    RETURN true;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.get_current_recovery_state(
    p_incident_id uuid
) RETURNS text AS $$
DECLARE
    v_status text;
BEGIN
    SELECT status INTO v_status
    FROM gptbridge_index.recovery_incident
    WHERE incident_id = p_incident_id;
    RETURN upper(COALESCE(v_status, 'HEALTHY'));
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
