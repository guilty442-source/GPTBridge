-- 120_shutdown_phase.sql
-- Shutdown Phase Sequence.
--
-- Shutdown is the reverse of startup:
--   STOP ACCEPTING NEW WORK → DRAIN TRANSPORT → STOP BACKGROUND JOBS →
--   CHECKPOINT WORKFLOWS → FLUSH AUDIT → FLUSH LOCAL STATE →
--   CLOSE QDRANT CLIENT → CLOSE SQLITE → CLOSE POSTGRES POOLS → STOP
--
-- PG pool closes last because audit/checkpoint/reconcile still need it.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

CREATE TABLE IF NOT EXISTS gptbridge_index.shutdown_phase (
    phase_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phase_number integer NOT NULL UNIQUE,
    phase_name text NOT NULL,
    description text,
    actions text[] NOT NULL DEFAULT '{}',
    timeout_seconds integer NOT NULL DEFAULT 300,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.shutdown_phase ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.shutdown_phase FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS shutdown_phase_read ON gptbridge_index.shutdown_phase;
CREATE POLICY shutdown_phase_read ON gptbridge_index.shutdown_phase
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS shutdown_phase_write ON gptbridge_index.shutdown_phase;
CREATE POLICY shutdown_phase_write ON gptbridge_index.shutdown_phase
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.shutdown_phase FROM PUBLIC;
GRANT SELECT ON gptbridge_index.shutdown_phase TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.shutdown_phase
    TO gptbridge_index_executor;

-- Pre-populate the 10 shutdown phases
INSERT INTO gptbridge_index.shutdown_phase (
    phase_number, phase_name, description, actions, timeout_seconds
) VALUES
    (1, 'STOP_ACCEPTING_NEW_WORK', 'Set accept_new_requests = false, allow in-flight to finish',
     ARRAY['set_accept_new_requests_false', 'allow_inflight_timeout'], 60),
    (2, 'DRAIN_TRANSPORT', 'Pending stays durable, claimed tasks finish, long tasks checkpoint',
     ARRAY['preserve_pending', 'finish_claimed', 'checkpoint_long_tasks', 'preserve_lease'], 120),
    (3, 'STOP_BACKGROUND_JOBS', 'Stop reconcile workers, background embedding, reindex',
     ARRAY['stop_reconcile_workers', 'stop_embedding', 'stop_reindex'], 60),
    (4, 'CHECKPOINT_WORKFLOWS', 'Persist workflow state, cursor, generation',
     ARRAY['persist_workflow_state', 'persist_cursor', 'persist_generation'], 60),
    (5, 'FLUSH_AUDIT', 'Flush all pending audit events to PostgreSQL',
     ARRAY['flush_audit_events', 'verify_audit_chain'], 30),
    (6, 'FLUSH_LOCAL_STATE', 'Flush SQLite pending state, commit, checkpoint per class policy',
     ARRAY['flush_sqlite_pending', 'commit_sqlite', 'checkpoint_per_class'], 60),
    (7, 'CLOSE_QDRANT_CLIENT', 'Save last completed batch, collection generation, cursor',
     ARRAY['save_qdrant_cursor', 'save_collection_generation', 'close_qdrant_client'], 30),
    (8, 'CLOSE_SQLITE', 'Close SQLite connections per class (no aggressive TRUNCATE)',
     ARRAY['close_sqlite_class_a', 'close_sqlite_class_b', 'close_sqlite_class_c', 'close_sqlite_class_d'], 30),
    (9, 'CLOSE_POSTGRES_POOLS', 'Close PG pools last (audit/checkpoint still needed during shutdown)',
     ARRAY['close_pg_pools'], 30),
    (10, 'STOP', 'Process exit',
     ARRAY['write_shutdown_audit', 'process_exit'], 10)
ON CONFLICT (phase_number) DO NOTHING;

CREATE OR REPLACE FUNCTION gptbridge_index.get_shutdown_order()
RETURNS TABLE (
    phase_number integer, phase_name text,
    actions text[], timeout_seconds integer
) AS $$
BEGIN
    RETURN QUERY
    SELECT phase_number, phase_name, actions, timeout_seconds
    FROM gptbridge_index.shutdown_phase
    ORDER BY phase_number;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
