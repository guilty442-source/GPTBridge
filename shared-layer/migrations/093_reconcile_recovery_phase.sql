-- 091_reconcile_recovery_phase.sql
-- Reconcile Recovery Phase.
--
-- After PG recovery verification, begin:
--   SQLite pending state → freeze fallback mutation window →
--   snapshot pending set → incremental reconcile → conflict detection → verification
--
-- SQLite → PostgreSQL single-direction reconcile boundary maintained.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A44/E30 — four-functions-local.

CREATE TABLE IF NOT EXISTS gptbridge_index.reconcile_recovery_phase (
    phase_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    status text NOT NULL DEFAULT 'snapshotting' CHECK (status IN (
        'snapshotting', 'reconciling', 'conflict_detecting',
        'verifying', 'completed', 'failed'
    )),
    pending_snapshot_count integer NOT NULL DEFAULT 0,
    reconciled_count integer NOT NULL DEFAULT 0,
    conflict_count integer NOT NULL DEFAULT 0,
    verified_count integer NOT NULL DEFAULT 0,
    last_processed_revision integer,
    batch_cursor text,
    conflict_details jsonb DEFAULT '[]'::jsonb,
    failure_reason text
);

ALTER TABLE gptbridge_index.reconcile_recovery_phase ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.reconcile_recovery_phase FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS reconcile_recovery_read ON gptbridge_index.reconcile_recovery_phase;
CREATE POLICY reconcile_recovery_read ON gptbridge_index.reconcile_recovery_phase
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS reconcile_recovery_write ON gptbridge_index.reconcile_recovery_phase;
CREATE POLICY reconcile_recovery_write ON gptbridge_index.reconcile_recovery_phase
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.reconcile_recovery_phase FROM PUBLIC;
GRANT SELECT ON gptbridge_index.reconcile_recovery_phase TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.reconcile_recovery_phase
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS reconcile_recovery_incident_idx
    ON gptbridge_index.reconcile_recovery_phase (incident_id, status);

CREATE OR REPLACE FUNCTION gptbridge_index.start_reconcile_recovery(
    p_incident_id uuid,
    p_pending_count integer
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.reconcile_recovery_phase (
        incident_id, pending_snapshot_count
    )
    VALUES (p_incident_id, p_pending_count)
    RETURNING phase_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.advance_reconcile_recovery(
    p_phase_id uuid,
    p_new_status text,
    p_reconciled integer DEFAULT 0,
    p_conflicts integer DEFAULT 0,
    p_verified integer DEFAULT 0,
    p_last_revision integer DEFAULT NULL,
    p_batch_cursor text DEFAULT NULL,
    p_failure_reason text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.reconcile_recovery_phase
    SET status = p_new_status,
        reconciled_count = reconciled_count + p_reconciled,
        conflict_count = conflict_count + p_conflicts,
        verified_count = verified_count + p_verified,
        last_processed_revision = COALESCE(p_last_revision, last_processed_revision),
        batch_cursor = COALESCE(p_batch_cursor, batch_cursor),
        completed_at = CASE WHEN p_new_status IN ('completed', 'failed') THEN now()
                            ELSE completed_at END,
        failure_reason = p_failure_reason
    WHERE phase_id = p_phase_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.is_reconcile_complete(
    p_incident_id uuid
) RETURNS boolean AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM gptbridge_index.reconcile_recovery_phase
        WHERE incident_id = p_incident_id AND status = 'completed'
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
