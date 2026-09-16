-- 094_transport_recovery.sql
-- Transport Recovery.
--
-- During incident, "don't know if SQL committed". Transport must use:
--   idempotency_key, request_id, operation_id
-- Recovery: first check if completed, not completed, or unknown.
-- Cannot blindly retry.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.transport_recovery (
    recovery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    request_id text,  -- links to transport tool_request
    idempotency_key text NOT NULL,
    operation_id text,
    resource_revision integer,
    commit_state text NOT NULL DEFAULT 'UNKNOWN' CHECK (commit_state IN (
        'COMMITTED', 'NOT_COMMITTED', 'UNKNOWN'
    )),
    detected_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolved_state text CHECK (resolved_state IN (
        'COMMITTED', 'NOT_COMMITTED', 'RETRIED', 'ABANDONED'
    )),
    resolution_method text,  -- 'authoritative_lookup', 'retry', 'manual'
    resolution_notes text
);

ALTER TABLE gptbridge_index.transport_recovery ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.transport_recovery FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS transport_recovery_read ON gptbridge_index.transport_recovery;
CREATE POLICY transport_recovery_read ON gptbridge_index.transport_recovery
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS transport_recovery_write ON gptbridge_index.transport_recovery;
CREATE POLICY transport_recovery_write ON gptbridge_index.transport_recovery
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.transport_recovery FROM PUBLIC;
GRANT SELECT ON gptbridge_index.transport_recovery TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.transport_recovery
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS transport_recovery_idempotency_idx
    ON gptbridge_index.transport_recovery (idempotency_key, commit_state);
CREATE INDEX IF NOT EXISTS transport_recovery_unknown_idx
    ON gptbridge_index.transport_recovery (commit_state, detected_at)
    WHERE commit_state = 'UNKNOWN';

CREATE OR REPLACE FUNCTION gptbridge_index.register_unknown_commit(
    p_incident_id uuid,
    p_idempotency_key text,
    p_request_id text DEFAULT NULL,
    p_operation_id text DEFAULT NULL,
    p_resource_revision integer DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.transport_recovery (
        incident_id, idempotency_key, request_id,
        operation_id, resource_revision, commit_state
    )
    VALUES (
        p_incident_id, p_idempotency_key, p_request_id,
        p_operation_id, p_resource_revision, 'UNKNOWN'
    )
    RETURNING recovery_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.resolve_commit_state(
    p_recovery_id uuid,
    p_resolved_state text,
    p_resolution_method text,
    p_resolution_notes text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.transport_recovery
    SET resolved_state = p_resolved_state,
        resolution_method = p_resolution_method,
        resolution_notes = p_resolution_notes,
        resolved_at = now(),
        commit_state = CASE WHEN p_resolved_state IN ('COMMITTED', 'NOT_COMMITTED')
                            THEN p_resolved_state ELSE commit_state END
    WHERE recovery_id = p_recovery_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.get_unknown_commits(
    p_incident_id uuid DEFAULT NULL
) RETURNS TABLE (
    recovery_id uuid, idempotency_key text, request_id text,
    commit_state text, detected_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT recovery_id, idempotency_key, request_id,
           commit_state, detected_at
    FROM gptbridge_index.transport_recovery
    WHERE commit_state = 'UNKNOWN' AND resolved_at IS NULL
      AND (p_incident_id IS NULL OR incident_id = p_incident_id)
    ORDER BY detected_at;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
