-- 106_recovery_checkpoint.sql
-- Recovery Checkpoint.
--
-- Long recovery work must be interruptible and resumable.
-- Save: recovery_run_id, current_phase, last_completed_step,
--        last_processed_revision, batch_cursor, generation
-- Program restart → resume, not restart from scratch.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

CREATE TABLE IF NOT EXISTS gptbridge_index.recovery_checkpoint (
    checkpoint_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    recovery_run_id uuid NOT NULL,
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    current_phase text NOT NULL,
    last_completed_step text,
    last_processed_revision integer,
    batch_cursor text,
    generation integer,
    total_processed integer NOT NULL DEFAULT 0,
    total_failed integer NOT NULL DEFAULT 0,
    saved_at timestamptz NOT NULL DEFAULT now(),
    resumed_at timestamptz,
    resumed_count integer NOT NULL DEFAULT 0,
    status text NOT NULL DEFAULT 'active' CHECK (status IN (
        'active', 'paused', 'resumed', 'completed', 'abandoned'
    ))
);

ALTER TABLE gptbridge_index.recovery_checkpoint ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.recovery_checkpoint FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS recovery_checkpoint_read ON gptbridge_index.recovery_checkpoint;
CREATE POLICY recovery_checkpoint_read ON gptbridge_index.recovery_checkpoint
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS recovery_checkpoint_write ON gptbridge_index.recovery_checkpoint;
CREATE POLICY recovery_checkpoint_write ON gptbridge_index.recovery_checkpoint
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.recovery_checkpoint FROM PUBLIC;
GRANT SELECT ON gptbridge_index.recovery_checkpoint TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.recovery_checkpoint
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS recovery_checkpoint_run_idx
    ON gptbridge_index.recovery_checkpoint (recovery_run_id, saved_at DESC);
CREATE INDEX IF NOT EXISTS recovery_checkpoint_active_idx
    ON gptbridge_index.recovery_checkpoint (status, saved_at)
    WHERE status = 'active';

CREATE OR REPLACE FUNCTION gptbridge_index.save_recovery_checkpoint(
    p_recovery_run_id uuid,
    p_incident_id uuid,
    p_current_phase text,
    p_last_completed_step text DEFAULT NULL,
    p_last_processed_revision integer DEFAULT NULL,
    p_batch_cursor text DEFAULT NULL,
    p_generation integer DEFAULT NULL,
    p_total_processed integer DEFAULT 0,
    p_total_failed integer DEFAULT 0
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.recovery_checkpoint (
        recovery_run_id, incident_id, current_phase,
        last_completed_step, last_processed_revision,
        batch_cursor, generation, total_processed, total_failed
    )
    VALUES (
        p_recovery_run_id, p_incident_id, p_current_phase,
        p_last_completed_step, p_last_processed_revision,
        p_batch_cursor, p_generation, p_total_processed, p_total_failed
    )
    RETURNING checkpoint_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.resume_recovery_checkpoint(
    p_checkpoint_id uuid
) RETURNS TABLE (
    current_phase text, last_completed_step text,
    last_processed_revision integer, batch_cursor text, generation integer
) AS $$
BEGIN
    UPDATE gptbridge_index.recovery_checkpoint
    SET status = 'resumed', resumed_at = now(), resumed_count = resumed_count + 1
    WHERE checkpoint_id = p_checkpoint_id AND status = 'active';

    RETURN QUERY
    SELECT current_phase, last_completed_step,
           last_processed_revision, batch_cursor, generation
    FROM gptbridge_index.recovery_checkpoint
    WHERE checkpoint_id = p_checkpoint_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.get_latest_checkpoint(
    p_recovery_run_id uuid
) RETURNS TABLE (
    checkpoint_id uuid, current_phase text,
    last_completed_step text, last_processed_revision integer,
    batch_cursor text, status text
) AS $$
BEGIN
    RETURN QUERY
    SELECT checkpoint_id, current_phase, last_completed_step,
           last_processed_revision, batch_cursor, status
    FROM gptbridge_index.recovery_checkpoint
    WHERE recovery_run_id = p_recovery_run_id AND status = 'active'
    ORDER BY saved_at DESC LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
