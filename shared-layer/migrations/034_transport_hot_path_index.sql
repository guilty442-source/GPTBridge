-- 034_transport_hot_path_index.sql
-- Transport Hot-Path Optimization: composite index matching the claim
-- query path, and a completed-request archive to keep hot queries fast.
--
-- The claim query path is:
--   WHERE channel_id = ? AND target_tool_id = ? AND status = 'queued'
--   AND (next_retry_at IS NULL OR next_retry_at <= now())
--   ORDER BY created_at, request_id LIMIT 1 FOR UPDATE SKIP LOCKED
--
-- The optimal index covers (channel_id, target_tool_id, status, created_at)
-- so the claim path is a single index scan, not a full table scan.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- Composite index for the transport claim path
-- ============================================================================
CREATE INDEX IF NOT EXISTS tool_request_claim_path_idx
    ON gptbridge_transport.tool_request (
        channel_id, target_tool_id, status, created_at, request_id
    )
    WHERE status IN ('queued', 'claimed');

-- ============================================================================
-- Index for expired-lease reclaim scan
-- ============================================================================
CREATE INDEX IF NOT EXISTS tool_request_reclaim_idx
    ON gptbridge_transport.tool_request (status, lease_until)
    WHERE status = 'claimed' AND lease_until IS NOT NULL;

-- ============================================================================
-- Index for dead-letter sweep
-- ============================================================================
CREATE INDEX IF NOT EXISTS tool_request_dead_sweep_idx
    ON gptbridge_transport.tool_request (status, dead_letter_at)
    WHERE status = 'dead-letter';

-- ============================================================================
-- Archive completed/cancelled requests older than a threshold to keep
-- the hot table small.  The runtime moves rows to tool_request_history.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_transport.tool_request_history (
    LIKE gptbridge_transport.tool_request INCLUDING ALL
);

ALTER TABLE gptbridge_transport.tool_request_history
    ADD COLUMN IF NOT EXISTS archived_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE gptbridge_transport.tool_request_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_transport.tool_request_history FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tool_request_history_read ON gptbridge_transport.tool_request_history;
CREATE POLICY tool_request_history_read ON gptbridge_transport.tool_request_history
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

REVOKE ALL ON gptbridge_transport.tool_request_history FROM PUBLIC;
GRANT SELECT ON gptbridge_transport.tool_request_history TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_transport.tool_request_history
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS tool_request_history_archived_idx
    ON gptbridge_transport.tool_request_history (archived_at);
CREATE INDEX IF NOT EXISTS tool_request_history_status_idx
    ON gptbridge_transport.tool_request_history (status, archived_at);

-- ============================================================================
-- archive_completed_requests() — move completed/cancelled requests older
-- than the threshold to the history table.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_transport.archive_completed_requests(
    p_older_than_hours integer DEFAULT 24,
    p_batch_limit integer DEFAULT 500
) RETURNS integer AS $$
DECLARE
    v_count integer;
BEGIN
    WITH moved AS (
        DELETE FROM gptbridge_transport.tool_request
        WHERE status IN ('completed', 'cancelled')
          AND updated_at < now() - (p_older_than_hours || ' hours')::interval
        RETURNING *
    )
    INSERT INTO gptbridge_transport.tool_request_history
    SELECT *, now() FROM moved;

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, gptbridge_transport, gptbridge_security;
