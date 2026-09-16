-- 097_sqlite_fallback_freeze.sql
-- SQLite Fallback Freeze.
--
-- During PG recovery reconcile, prevent SQLite from continuously producing
-- new pending data. Phases:
--   fallback_open → fallback_draining → fallback_frozen → fallback_closed
--
-- Codex basis:
--   A44/E30 — four-functions-local.
--   A8/E21  — PostgreSQL: central-structured-official-data.

CREATE TABLE IF NOT EXISTS gptbridge_index.sqlite_fallback_freeze (
    freeze_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    module_id text NOT NULL,
    fallback_state text NOT NULL DEFAULT 'fallback_open' CHECK (fallback_state IN (
        'fallback_open', 'fallback_draining', 'fallback_frozen', 'fallback_closed'
    )),
    entered_at timestamptz NOT NULL DEFAULT now(),
    transitioned_at timestamptz,
    transitioned_by text,
    pending_count integer NOT NULL DEFAULT 0,
    drained_count integer NOT NULL DEFAULT 0,
    notes text
);

ALTER TABLE gptbridge_index.sqlite_fallback_freeze ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.sqlite_fallback_freeze FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sqlite_fallback_freeze_read ON gptbridge_index.sqlite_fallback_freeze;
CREATE POLICY sqlite_fallback_freeze_read ON gptbridge_index.sqlite_fallback_freeze
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS sqlite_fallback_freeze_write ON gptbridge_index.sqlite_fallback_freeze;
CREATE POLICY sqlite_fallback_freeze_write ON gptbridge_index.sqlite_fallback_freeze
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.sqlite_fallback_freeze FROM PUBLIC;
GRANT SELECT ON gptbridge_index.sqlite_fallback_freeze TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.sqlite_fallback_freeze
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS sqlite_fallback_state_idx
    ON gptbridge_index.sqlite_fallback_freeze (fallback_state, module_id);

CREATE OR REPLACE FUNCTION gptbridge_index.transition_fallback_state(
    p_freeze_id uuid,
    p_new_state text,
    p_transitioned_by text,
    p_drained_count integer DEFAULT 0
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.sqlite_fallback_freeze
    SET fallback_state = p_new_state,
        transitioned_at = now(),
        transitioned_by = p_transitioned_by,
        drained_count = drained_count + p_drained_count
    WHERE freeze_id = p_freeze_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.get_fallback_state(
    p_module_id text
) RETURNS text AS $$
DECLARE
    v_state text;
BEGIN
    SELECT fallback_state INTO v_state
    FROM gptbridge_index.sqlite_fallback_freeze
    WHERE module_id = p_module_id
    ORDER BY entered_at DESC LIMIT 1;
    RETURN COALESCE(v_state, 'fallback_open');
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
