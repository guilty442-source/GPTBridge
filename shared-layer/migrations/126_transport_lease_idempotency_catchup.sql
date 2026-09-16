-- 114_transport_lease_idempotency_catchup.sql
-- Brings a drifted live database up to the transport columns the runtime
-- writes (equivalent to the additive parts of 007 + 010 on installations
-- where those migrations were never applied).
--
-- Additive and idempotent: safe on both drifted and fully migrated DBs.

ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS idempotency_key text;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS processed_at timestamptz;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS response_payload jsonb;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS claimed_at timestamptz;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS lease_until timestamptz;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS attempt_count integer NOT NULL DEFAULT 0;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS next_retry_at timestamptz;

CREATE UNIQUE INDEX IF NOT EXISTS tool_request_idempotency_idx
    ON gptbridge_transport.tool_request (target_tool_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS tool_request_idempotency_lookup_idx
    ON gptbridge_transport.tool_request (idempotency_key, target_tool_id)
    WHERE idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS tool_request_expired_lease_idx
    ON gptbridge_transport.tool_request (channel_id, target_tool_id, status, lease_until)
    WHERE status = 'claimed';

CREATE INDEX IF NOT EXISTS tool_request_retry_idx
    ON gptbridge_transport.tool_request (channel_id, target_tool_id, status, next_retry_at)
    WHERE status = 'queued';
