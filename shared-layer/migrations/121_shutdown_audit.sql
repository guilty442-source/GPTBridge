-- 121_shutdown_audit.sql
-- Shutdown Audit.
--
-- Last thing before process exit, record:
--   shutdown_id, started_at, drain_result, pending_operations,
--   reconcile_pending, database_generation, completed_at, shutdown_status
--
-- Next startup can know: was last shutdown graceful or unclean?
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.shutdown_audit (
    shutdown_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    started_at timestamptz NOT NULL,
    completed_at timestamptz,
    shutdown_status text NOT NULL DEFAULT 'in_progress' CHECK (shutdown_status IN (
        'in_progress', 'graceful', 'forced', 'unclean', 'timeout'
    )),
    drain_result text CHECK (drain_result IN (
        'all_drained', 'partial_drain', 'timeout', 'forced_release'
    )),
    pending_operations integer NOT NULL DEFAULT 0,
    pending_transport integer NOT NULL DEFAULT 0,
    reconcile_pending integer NOT NULL DEFAULT 0,
    database_generation integer,
    security_generation integer,
    active_leases_released integer NOT NULL DEFAULT 0,
    active_leases_expired integer NOT NULL DEFAULT 0,
    sqlite_checkpoints_done integer NOT NULL DEFAULT 0,
    qdrant_cursor_saved boolean NOT NULL DEFAULT false,
    audit_flushed boolean NOT NULL DEFAULT false,
    notes text
);

ALTER TABLE gptbridge_index.shutdown_audit ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.shutdown_audit FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS shutdown_audit_read ON gptbridge_index.shutdown_audit;
CREATE POLICY shutdown_audit_read ON gptbridge_index.shutdown_audit
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS shutdown_audit_write ON gptbridge_index.shutdown_audit;
CREATE POLICY shutdown_audit_write ON gptbridge_index.shutdown_audit
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.shutdown_audit FROM PUBLIC;
GRANT SELECT ON gptbridge_index.shutdown_audit TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.shutdown_audit
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS shutdown_audit_status_idx
    ON gptbridge_index.shutdown_audit (shutdown_status, started_at);

CREATE OR REPLACE FUNCTION gptbridge_index.start_shutdown_audit()
RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_gen integer;
BEGIN
    v_gen := gptbridge_index.get_current_generation();
    INSERT INTO gptbridge_index.shutdown_audit (started_at, database_generation)
    VALUES (now(), v_gen)
    RETURNING shutdown_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.complete_shutdown_audit(
    p_shutdown_id uuid,
    p_status text,
    p_drain_result text DEFAULT NULL,
    p_pending_ops integer DEFAULT 0,
    p_pending_transport integer DEFAULT 0,
    p_reconcile_pending integer DEFAULT 0,
    p_leases_released integer DEFAULT 0,
    p_leases_expired integer DEFAULT 0,
    p_sqlite_checkpoints integer DEFAULT 0,
    p_qdrant_cursor_saved boolean DEFAULT false,
    p_audit_flushed boolean DEFAULT false,
    p_notes text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.shutdown_audit
    SET shutdown_status = p_status,
        completed_at = now(),
        drain_result = p_drain_result,
        pending_operations = p_pending_ops,
        pending_transport = p_pending_transport,
        reconcile_pending = p_reconcile_pending,
        active_leases_released = p_leases_released,
        active_leases_expired = p_leases_expired,
        sqlite_checkpoints_done = p_sqlite_checkpoints,
        qdrant_cursor_saved = p_qdrant_cursor_saved,
        audit_flushed = p_audit_flushed,
        notes = p_notes
    WHERE shutdown_id = p_shutdown_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.was_last_shutdown_graceful()
RETURNS boolean AS $$
DECLARE
    v_status text;
BEGIN
    SELECT shutdown_status INTO v_status
    FROM gptbridge_index.shutdown_audit
    ORDER BY started_at DESC LIMIT 1;
    RETURN COALESCE(v_status = 'graceful', false);
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
