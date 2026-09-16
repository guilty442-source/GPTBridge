-- 122_unclean_shutdown_detection.sql
-- Unclean Shutdown Detection.
--
-- If last shutdown was not graceful, startup must do extra:
--   transport lease recovery
--   unknown commit verification
--   SQLite WAL verification
--   reconcile state verification
--   operation state recovery
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A10/E10 — explicit-allowlist.

CREATE TABLE IF NOT EXISTS gptbridge_index.unclean_shutdown_detection (
    detection_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    detected_at timestamptz NOT NULL DEFAULT now(),
    last_shutdown_id uuid REFERENCES gptbridge_index.shutdown_audit(shutdown_id),
    was_graceful boolean NOT NULL DEFAULT false,
    extra_recovery_steps text[] NOT NULL DEFAULT '{}',
    steps_completed text[] NOT NULL DEFAULT '{}',
    all_steps_done boolean NOT NULL DEFAULT false,
    completed_at timestamptz
);

ALTER TABLE gptbridge_index.unclean_shutdown_detection ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.unclean_shutdown_detection FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS unclean_detect_read ON gptbridge_index.unclean_shutdown_detection;
CREATE POLICY unclean_detect_read ON gptbridge_index.unclean_shutdown_detection
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS unclean_detect_write ON gptbridge_index.unclean_shutdown_detection;
CREATE POLICY unclean_detect_write ON gptbridge_index.unclean_shutdown_detection
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.unclean_shutdown_detection FROM PUBLIC;
GRANT SELECT ON gptbridge_index.unclean_shutdown_detection TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.unclean_shutdown_detection
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS unclean_detect_at_idx
    ON gptbridge_index.unclean_shutdown_detection (detected_at);

CREATE OR REPLACE FUNCTION gptbridge_index.detect_unclean_shutdown()
RETURNS TABLE (
    was_graceful boolean,
    extra_steps text[]
) AS $$
DECLARE
    v_graceful boolean;
    v_shutdown_id uuid;
    v_steps text[];
BEGIN
    v_graceful := gptbridge_index.was_last_shutdown_graceful();

    SELECT shutdown_id INTO v_shutdown_id
    FROM gptbridge_index.shutdown_audit
    ORDER BY started_at DESC LIMIT 1;

    IF NOT v_graceful THEN
        v_steps := ARRAY[
            'transport_lease_recovery',
            'unknown_commit_verification',
            'sqlite_wal_verification',
            'reconcile_state_verification',
            'operation_state_recovery'
        ];
    ELSE
        v_steps := ARRAY[]::text[];
    END IF;

    INSERT INTO gptbridge_index.unclean_shutdown_detection (
        last_shutdown_id, was_graceful, extra_recovery_steps
    )
    VALUES (v_shutdown_id, v_graceful, v_steps);

    RETURN QUERY SELECT v_graceful, v_steps;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.mark_unclean_step_done(
    p_detection_id uuid,
    p_step_name text
) RETURNS void AS $$
DECLARE
    v_steps text[];
    v_all text[];
BEGIN
    SELECT extra_recovery_steps INTO v_all
    FROM gptbridge_index.unclean_shutdown_detection
    WHERE detection_id = p_detection_id;

    SELECT steps_completed INTO v_steps
    FROM gptbridge_index.unclean_shutdown_detection
    WHERE detection_id = p_detection_id;

    v_steps := array_append(v_steps, p_step_name);

    UPDATE gptbridge_index.unclean_shutdown_detection
    SET steps_completed = v_steps,
        all_steps_done = (v_steps @> v_all),
        completed_at = CASE WHEN v_steps @> v_all THEN now() ELSE completed_at END
    WHERE detection_id = p_detection_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.is_unclean_recovery_complete()
RETURNS boolean AS $$
BEGIN
    RETURN COALESCE(
        (SELECT all_steps_done FROM gptbridge_index.unclean_shutdown_detection
         ORDER BY detected_at DESC LIMIT 1),
        true  -- if no detection record, assume clean
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
