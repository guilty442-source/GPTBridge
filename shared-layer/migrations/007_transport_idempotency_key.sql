-- 007_transport_idempotency_key.sql
-- Add idempotency_key to gptbridge_transport.tool_request.
-- Prevents duplicate execution when a cross-tool request is retried.
-- A8/E21 + A46/E22.

-- Add idempotency_key column (nullable for backward compatibility with
-- pre-existing rows; new requests MUST set it).
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS idempotency_key text;

-- Unique constraint: one request per (target_tool_id, idempotency_key)
-- This ensures that a retry with the same idempotency_key is rejected
-- or returns the original response instead of executing twice.
CREATE UNIQUE INDEX IF NOT EXISTS tool_request_idempotency_idx
    ON gptbridge_transport.tool_request (target_tool_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Add processed_at and response_payload for idempotent replay
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS processed_at timestamptz;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS response_payload jsonb;

-- Index for idempotency lookup
CREATE INDEX IF NOT EXISTS tool_request_idempotency_lookup_idx
    ON gptbridge_transport.tool_request (idempotency_key, target_tool_id)
    WHERE idempotency_key IS NOT NULL;
