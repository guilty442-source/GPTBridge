-- 087_recovery_incident.sql
-- Recovery Incident Tracking.
--
-- Tracks each incident through:
--   detect → classify → freeze → degrade → recover → reconcile → verify → resume
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.recovery_incident (
    incident_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id text REFERENCES gptbridge_index.recovery_plan(plan_id),
    incident_type text NOT NULL,
    detected_at timestamptz NOT NULL DEFAULT now(),
    detected_by text NOT NULL,
    description text,
    affected_components text[] NOT NULL DEFAULT '{}',
    severity text NOT NULL DEFAULT 'minor' CHECK (severity IN (
        'minor', 'major', 'critical', 'catastrophic'
    )),
    status text NOT NULL DEFAULT 'detected' CHECK (status IN (
        'detected', 'classified', 'freezing', 'degraded',
        'recovering', 'reconciling', 'verifying', 'resolved',
        'quarantined', 'restore_required', 'manual_intervention'
    )),
    recovery_generation integer,
    degraded_generation integer,
    resolved_generation integer,
    resolved_at timestamptz,
    resolved_by text,
    resolution_notes text,
    incident_log jsonb NOT NULL DEFAULT '[]'::jsonb,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.recovery_incident ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.recovery_incident FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS recovery_incident_read ON gptbridge_index.recovery_incident;
CREATE POLICY recovery_incident_read ON gptbridge_index.recovery_incident
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS recovery_incident_write ON gptbridge_index.recovery_incident;
CREATE POLICY recovery_incident_write ON gptbridge_index.recovery_incident
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.recovery_incident FROM PUBLIC;
GRANT SELECT ON gptbridge_index.recovery_incident TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.recovery_incident
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS recovery_incident_status_idx
    ON gptbridge_index.recovery_incident (status, detected_at);
CREATE INDEX IF NOT EXISTS recovery_incident_type_idx
    ON gptbridge_index.recovery_incident (incident_type, detected_at);

CREATE OR REPLACE FUNCTION gptbridge_index.open_recovery_incident(
    p_plan_id text,
    p_incident_type text,
    p_detected_by text,
    p_description text DEFAULT NULL,
    p_affected_components text[] DEFAULT '{}',
    p_severity text DEFAULT 'minor'
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.recovery_incident (
        plan_id, incident_type, detected_by, description,
        affected_components, severity, status
    )
    VALUES (
        p_plan_id, p_incident_type, p_detected_by, p_description,
        p_affected_components, p_severity, 'detected'
    )
    RETURNING incident_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.advance_incident_status(
    p_incident_id uuid,
    p_new_status text,
    p_notes text DEFAULT NULL
) RETURNS void AS $$
DECLARE
    v_log jsonb;
BEGIN
    SELECT incident_log INTO v_log
    FROM gptbridge_index.recovery_incident WHERE incident_id = p_incident_id;
    v_log := v_log || jsonb_build_object(
        'status', p_new_status, 'at', now()::text, 'notes', p_notes
    );
    UPDATE gptbridge_index.recovery_incident
    SET status = p_new_status, incident_log = v_log,
        resolved_at = CASE WHEN p_new_status = 'resolved' THEN now() ELSE resolved_at END,
        updated_at = now()
    WHERE incident_id = p_incident_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.get_active_incidents()
RETURNS TABLE (
    incident_id uuid, incident_type text, severity text,
    status text, detected_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT incident_id, incident_type, severity, status, detected_at
    FROM gptbridge_index.recovery_incident
    WHERE status NOT IN ('resolved', 'quarantined', 'manual_intervention')
    ORDER BY detected_at DESC;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
