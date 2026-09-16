-- 099_qdrant_recovery.sql
-- Qdrant Recovery.
--
-- Qdrant offline → semantic capability degraded → PostgreSQL remains authority →
-- record indexing backlog. Recovery: PG chunk metadata → find missing/stale →
-- rebuild → verify point IDs → update index_state.
-- Never reverse: don't use Qdrant to fix PostgreSQL.
--
-- Codex basis:
--   A52/E38 — RAG: Qdrant canonical semantic index.
--   A8/E21  — PostgreSQL: central-structured-official-data.

CREATE TABLE IF NOT EXISTS gptbridge_index.qdrant_recovery (
    recovery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    detected_at timestamptz NOT NULL DEFAULT now(),
    detected_by text NOT NULL,
    qdrant_status text NOT NULL DEFAULT 'offline' CHECK (qdrant_status IN (
        'offline', 'degraded', 'recovering', 'rebuilding', 'verified', 'failed'
    )),
    indexing_backlog_count integer NOT NULL DEFAULT 0,
    missing_points_count integer NOT NULL DEFAULT 0,
    stale_points_count integer NOT NULL DEFAULT 0,
    rebuilt_points_count integer NOT NULL DEFAULT 0,
    verified_points_count integer NOT NULL DEFAULT 0,
    recovered_at timestamptz,
    notes text
);

ALTER TABLE gptbridge_index.qdrant_recovery ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.qdrant_recovery FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS qdrant_recovery_read ON gptbridge_index.qdrant_recovery;
CREATE POLICY qdrant_recovery_read ON gptbridge_index.qdrant_recovery
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS qdrant_recovery_write ON gptbridge_index.qdrant_recovery;
CREATE POLICY qdrant_recovery_write ON gptbridge_index.qdrant_recovery
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.qdrant_recovery FROM PUBLIC;
GRANT SELECT ON gptbridge_index.qdrant_recovery TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.qdrant_recovery
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS qdrant_recovery_status_idx
    ON gptbridge_index.qdrant_recovery (qdrant_status, detected_at);

CREATE OR REPLACE FUNCTION gptbridge_index.start_qdrant_recovery(
    p_incident_id uuid,
    p_detected_by text,
    p_backlog_count integer DEFAULT 0
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.qdrant_recovery (
        incident_id, detected_by, indexing_backlog_count
    )
    VALUES (p_incident_id, p_detected_by, p_backlog_count)
    RETURNING recovery_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.update_qdrant_recovery(
    p_recovery_id uuid,
    p_new_status text,
    p_missing integer DEFAULT 0,
    p_stale integer DEFAULT 0,
    p_rebuilt integer DEFAULT 0,
    p_verified integer DEFAULT 0
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.qdrant_recovery
    SET qdrant_status = p_new_status,
        missing_points_count = missing_points_count + p_missing,
        stale_points_count = stale_points_count + p_stale,
        rebuilt_points_count = rebuilt_points_count + p_rebuilt,
        verified_points_count = verified_points_count + p_verified,
        recovered_at = CASE WHEN p_new_status = 'verified' THEN now() ELSE recovered_at END
    WHERE recovery_id = p_recovery_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
