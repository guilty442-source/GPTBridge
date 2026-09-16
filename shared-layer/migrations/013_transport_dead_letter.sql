-- 013_transport_dead_letter.sql
-- Transport Dead-Letter Queue: requests that exhaust their retry budget
-- are moved to 'dead-letter' status with full failure context, instead of
-- looping forever.
--
-- A8/E21 + A46/E22.

-- Add dead-letter columns to tool_request
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS dead_letter_reason text;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS dead_letter_at timestamptz;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS max_attempts integer NOT NULL DEFAULT 5;

-- Index for dead-letter queries
CREATE INDEX IF NOT EXISTS tool_request_dead_letter_idx
    ON gptbridge_transport.tool_request (channel_id, target_tool_id, status)
    WHERE status = 'dead-letter';

-- Function to move a request to dead-letter status.
-- Called by the transport layer when attempt_count >= max_attempts.
CREATE OR REPLACE FUNCTION gptbridge_transport.move_to_dead_letter(
    p_channel_id text,
    p_request_id text,
    p_reason text
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_transport.tool_request
    SET status = 'dead-letter',
        dead_letter_reason = p_reason,
        dead_letter_at = now(),
        updated_at = now()
    WHERE channel_id = p_channel_id
      AND request_id = p_request_id
      AND status IN ('queued', 'claimed');
END;
$$ LANGUAGE plpgsql;

-- Trigger: automatically move to dead-letter when attempt_count exceeds
-- max_attempts on the next claim cycle.
CREATE OR REPLACE FUNCTION gptbridge_transport.auto_dead_letter()
RETURNS trigger AS $$
BEGIN
    IF NEW.attempt_count >= NEW.max_attempts AND NEW.status = 'queued' THEN
        NEW.status = 'dead-letter';
        NEW.dead_letter_reason = 'max_attempts_exceeded';
        NEW.dead_letter_at = now();
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tool_request_auto_dead_letter ON gptbridge_transport.tool_request;
CREATE TRIGGER tool_request_auto_dead_letter
    BEFORE UPDATE OF attempt_count ON gptbridge_transport.tool_request
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_transport.auto_dead_letter();
