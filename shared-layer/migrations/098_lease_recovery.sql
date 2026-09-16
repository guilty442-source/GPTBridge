-- 096_lease_recovery.sql
-- Lease Recovery.
--
-- CLAIMED transport requests during incident must not be permanently stuck.
-- Use lease_until, claimed_at, worker_generation to determine if lease expired.
-- Expired → recoverable, re-claim. Preserve original attempt history.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.

CREATE TABLE IF NOT EXISTS gptbridge_index.lease_recovery (
    lease_recovery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    request_id text NOT NULL,
    original_worker text,
    original_claimed_at timestamptz,
    lease_until timestamptz,
    worker_generation integer,
    lease_status text NOT NULL DEFAULT 'checking' CHECK (lease_status IN (
        'checking', 'expired', 'active', 'reclaimed', 'abandoned'
    )),
    checked_at timestamptz NOT NULL DEFAULT now(),
    reclaimed_at timestamptz,
    reclaimed_by text,
    attempt_history jsonb NOT NULL DEFAULT '[]'::jsonb
);

ALTER TABLE gptbridge_index.lease_recovery ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.lease_recovery FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS lease_recovery_read ON gptbridge_index.lease_recovery;
CREATE POLICY lease_recovery_read ON gptbridge_index.lease_recovery
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS lease_recovery_write ON gptbridge_index.lease_recovery;
CREATE POLICY lease_recovery_write ON gptbridge_index.lease_recovery
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.lease_recovery FROM PUBLIC;
GRANT SELECT ON gptbridge_index.lease_recovery TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.lease_recovery
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS lease_recovery_status_idx
    ON gptbridge_index.lease_recovery (lease_status, checked_at);

CREATE OR REPLACE FUNCTION gptbridge_index.check_lease_expiry(
    p_incident_id uuid,
    p_request_id text,
    p_lease_until timestamptz,
    p_original_worker text DEFAULT NULL,
    p_original_claimed_at timestamptz DEFAULT NULL,
    p_worker_generation integer DEFAULT NULL
) RETURNS text AS $$
DECLARE
    v_id uuid;
    v_status text;
BEGIN
    IF p_lease_until < now() THEN
        v_status := 'expired';
    ELSE
        v_status := 'active';
    END IF;

    INSERT INTO gptbridge_index.lease_recovery (
        incident_id, request_id, original_worker,
        original_claimed_at, lease_until, worker_generation, lease_status
    )
    VALUES (
        p_incident_id, p_request_id, p_original_worker,
        p_original_claimed_at, p_lease_until, p_worker_generation, v_status
    )
    RETURNING lease_recovery_id INTO v_id;

    RETURN v_status;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.reclaim_lease(
    p_lease_recovery_id uuid,
    p_reclaimed_by text
) RETURNS void AS $$
DECLARE
    v_history jsonb;
BEGIN
    SELECT attempt_history INTO v_history
    FROM gptbridge_index.lease_recovery
    WHERE lease_recovery_id = p_lease_recovery_id;

    v_history := v_history || jsonb_build_object(
        'reclaimed_at', now()::text,
        'reclaimed_by', p_reclaimed_by
    );

    UPDATE gptbridge_index.lease_recovery
    SET lease_status = 'reclaimed',
        reclaimed_at = now(),
        reclaimed_by = p_reclaimed_by,
        attempt_history = v_history
    WHERE lease_recovery_id = p_lease_recovery_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.get_expired_leases(
    p_incident_id uuid DEFAULT NULL
) RETURNS TABLE (
    lease_recovery_id uuid, request_id text,
    lease_until timestamptz, lease_status text
) AS $$
BEGIN
    RETURN QUERY
    SELECT lease_recovery_id, request_id, lease_until, lease_status
    FROM gptbridge_index.lease_recovery
    WHERE lease_status = 'expired' AND reclaimed_at IS NULL
      AND (p_incident_id IS NULL OR incident_id = p_incident_id)
    ORDER BY lease_until;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
