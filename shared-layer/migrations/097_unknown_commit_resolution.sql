-- 095_unknown_commit_resolution.sql
-- Unknown Commit Resolution.
--
-- If UNKNOWN: do authoritative lookup by idempotency_key/operation_id/resource_revision
-- then decide whether to retry.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.unknown_commit_resolution (
    resolution_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    transport_recovery_id uuid REFERENCES gptbridge_index.transport_recovery(recovery_id),
    idempotency_key text NOT NULL,
    operation_id text,
    resource_revision integer,
    lookup_method text NOT NULL CHECK (lookup_method IN (
        'idempotency_key', 'operation_id', 'resource_revision', 'manual'
    )),
    lookup_result jsonb,
    found_committed boolean,
    resolved_state text NOT NULL CHECK (resolved_state IN (
        'COMMITTED', 'NOT_COMMITTED', 'RETRIED', 'ABANDONED'
    )),
    resolved_at timestamptz NOT NULL DEFAULT now(),
    resolved_by text NOT NULL,
    notes text
);

ALTER TABLE gptbridge_index.unknown_commit_resolution ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.unknown_commit_resolution FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS unknown_commit_res_read ON gptbridge_index.unknown_commit_resolution;
CREATE POLICY unknown_commit_res_read ON gptbridge_index.unknown_commit_resolution
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS unknown_commit_res_write ON gptbridge_index.unknown_commit_resolution;
CREATE POLICY unknown_commit_res_write ON gptbridge_index.unknown_commit_resolution
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.unknown_commit_resolution FROM PUBLIC;
GRANT SELECT ON gptbridge_index.unknown_commit_resolution TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.unknown_commit_resolution
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS unknown_commit_res_key_idx
    ON gptbridge_index.unknown_commit_resolution (idempotency_key);

CREATE OR REPLACE FUNCTION gptbridge_index.record_commit_lookup(
    p_transport_recovery_id uuid,
    p_idempotency_key text,
    p_lookup_method text,
    p_found_committed boolean,
    p_resolved_state text,
    p_resolved_by text,
    p_lookup_result jsonb DEFAULT NULL,
    p_operation_id text DEFAULT NULL,
    p_resource_revision integer DEFAULT NULL,
    p_notes text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.unknown_commit_resolution (
        transport_recovery_id, idempotency_key, operation_id,
        resource_revision, lookup_method, lookup_result,
        found_committed, resolved_state, resolved_by, notes
    )
    VALUES (
        p_transport_recovery_id, p_idempotency_key, p_operation_id,
        p_resource_revision, p_lookup_method, p_lookup_result,
        p_found_committed, p_resolved_state, p_resolved_by, p_notes
    )
    RETURNING resolution_id INTO v_id;

    PERFORM gptbridge_index.resolve_commit_state(
        p_transport_recovery_id, p_resolved_state, p_lookup_method, p_notes
    );

    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
