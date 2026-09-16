-- 041_transport_priority_queue.sql
-- Adaptive SQL layer: priority queue support for
-- gptbridge_transport.tool_request.
--
-- Every submitter declares a traffic class (critical / interactive /
-- background / maintenance); claim honours priority_value first, then FIFO,
-- and skips requests whose deadline has already passed.  Values come from
-- gptbridge_transport.priority_value_for() so Python and SQL agree.
--
-- Additive and idempotent: existing rows default to 'interactive'.

ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS priority_class text;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS priority_value integer;
ALTER TABLE gptbridge_transport.tool_request
    ADD COLUMN IF NOT EXISTS deadline_at timestamptz;

UPDATE gptbridge_transport.tool_request
    SET priority_class = 'interactive'
    WHERE priority_class IS NULL;

CREATE OR REPLACE FUNCTION gptbridge_transport.priority_value_for(p_class text)
    RETURNS integer
    LANGUAGE sql
    IMMUTABLE
AS $$
    SELECT CASE p_class
        WHEN 'critical' THEN 0
        WHEN 'interactive' THEN 100
        WHEN 'background' THEN 500
        WHEN 'maintenance' THEN 900
        ELSE 100
    END;
$$;

UPDATE gptbridge_transport.tool_request
    SET priority_value = gptbridge_transport.priority_value_for(priority_class)
    WHERE priority_value IS NULL;

ALTER TABLE gptbridge_transport.tool_request
    ALTER COLUMN priority_class SET DEFAULT 'interactive';
ALTER TABLE gptbridge_transport.tool_request
    ALTER COLUMN priority_class SET NOT NULL;
ALTER TABLE gptbridge_transport.tool_request
    ALTER COLUMN priority_value SET DEFAULT 100;
ALTER TABLE gptbridge_transport.tool_request
    ALTER COLUMN priority_value SET NOT NULL;

DO $$
BEGIN
    ALTER TABLE gptbridge_transport.tool_request
        ADD CONSTRAINT tool_request_priority_class_check
        CHECK (priority_class IN ('critical', 'interactive', 'background', 'maintenance'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;

-- Claim hot path: priority first, FIFO within a class, queued rows only.
CREATE INDEX IF NOT EXISTS tool_request_claim_priority_idx
    ON gptbridge_transport.tool_request
    (channel_id, target_tool_id, priority_value, created_at)
    WHERE status = 'queued';
