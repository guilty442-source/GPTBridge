-- 089_pg_offline_recovery.sql
-- PostgreSQL Offline Recovery.
--
-- PG connection failure → confirm repeated failure → open incident →
-- stop non-essential PG writes → evaluate manifest criticality →
-- eligible modules switch bounded SQLite fallback → record degraded generation
--
-- SQLite fallback ≠ PostgreSQL authority, just temporary bounded local state.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A44/E30 — four-functions-local.

CREATE TABLE IF NOT EXISTS gptbridge_index.pg_offline_recovery (
    recovery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    failure_detected_at timestamptz NOT NULL DEFAULT now(),
    confirmed_at timestamptz,  -- repeated failure confirmed
    confirmation_attempts integer NOT NULL DEFAULT 0,
    degraded_at timestamptz,
    recovered_at timestamptz,
    degraded_generation integer,
    recovered_generation integer,
    modules_on_fallback text[] NOT NULL DEFAULT '{}',
    fallback_status text NOT NULL DEFAULT 'pending' CHECK (fallback_status IN (
        'pending', 'confirmed', 'degraded', 'recovering', 'recovered'
    )),
    notes text
);

ALTER TABLE gptbridge_index.pg_offline_recovery ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.pg_offline_recovery FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS pg_offline_read ON gptbridge_index.pg_offline_recovery;
CREATE POLICY pg_offline_read ON gptbridge_index.pg_offline_recovery
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS pg_offline_write ON gptbridge_index.pg_offline_recovery;
CREATE POLICY pg_offline_write ON gptbridge_index.pg_offline_recovery
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.pg_offline_recovery FROM PUBLIC;
GRANT SELECT ON gptbridge_index.pg_offline_recovery TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.pg_offline_recovery
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS pg_offline_status_idx
    ON gptbridge_index.pg_offline_recovery (fallback_status, failure_detected_at);

CREATE OR REPLACE FUNCTION gptbridge_index.start_pg_offline_recovery(
    p_incident_id uuid,
    p_detected_by text
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.pg_offline_recovery (incident_id)
    VALUES (p_incident_id)
    RETURNING recovery_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.confirm_pg_failure(
    p_recovery_id uuid,
    p_required_attempts integer DEFAULT 3
) RETURNS boolean AS $$
DECLARE
    v_attempts integer;
BEGIN
    SELECT confirmation_attempts INTO v_attempts
    FROM gptbridge_index.pg_offline_recovery
    WHERE recovery_id = p_recovery_id;

    v_attempts := v_attempts + 1;

    UPDATE gptbridge_index.pg_offline_recovery
    SET confirmation_attempts = v_attempts,
        confirmed_at = CASE WHEN v_attempts >= p_required_attempts THEN now() ELSE confirmed_at END,
        fallback_status = CASE WHEN v_attempts >= p_required_attempts THEN 'confirmed' ELSE fallback_status END
    WHERE recovery_id = p_recovery_id;

    RETURN v_attempts >= p_required_attempts;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.enter_degraded_mode(
    p_recovery_id uuid,
    p_modules text[],
    p_degraded_generation integer
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.pg_offline_recovery
    SET fallback_status = 'degraded',
        degraded_at = now(),
        modules_on_fallback = p_modules,
        degraded_generation = p_degraded_generation
    WHERE recovery_id = p_recovery_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
