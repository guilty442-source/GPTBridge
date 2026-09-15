-- 010_transport_lease_columns.sql
-- A8/E21 + A46/E22: transport lease mechanism.
-- Prevents a request from being permanently stuck when a worker claims it
-- then crashes before responding.  Adds:
--   claimed_at     — when the current lease started
--   lease_until    — when the lease expires (claimed rows past this time
--                    are eligible for re-claim by another worker)
--   attempt_count  — monotonic per-request claim counter (1-based)
--   next_retry_at  — earliest time a failed request may be re-claimed
--
-- The existing SKIP LOCKED + idempotency_key path is preserved; the lease
-- columns extend it so expired leases are re-queueable without manual
-- intervention.

ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS lease_until timestamptz;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS next_retry_at timestamptz;

-- Index for expired-lease re-claim queries:
-- SELECT ... WHERE status='claimed' AND lease_until < now()
CREATE INDEX IF NOT EXISTS tool_request_expired_lease_idx
    ON gptbridge_transport.tool_request (channel_id, target_tool_id, status, lease_until)
    WHERE status = 'claimed';

-- Index for retry-eligible queries:
-- SELECT ... WHERE status='queued' AND (next_retry_at IS NULL OR next_retry_at <= now())
CREATE INDEX IF NOT EXISTS tool_request_retry_idx
    ON gptbridge_transport.tool_request (channel_id, target_tool_id, status, next_retry_at)
    WHERE status = 'queued';
