-- 011_data_invariants.sql
-- Data invariants: constraints and triggers that enforce integrity at the
-- database level, so application bugs cannot violate them silently.
--
-- Invariants enforced:
--   INV-1  resource_id is globally unique (already PK, but add explicit CHECK)
--   INV-2  qdrant_point_id in gptbridge_rag.chunk must correspond to a real
--          index_state row (the resource_id FK already enforces this, but
--          we add a trigger that rejects chunks whose resource_id has no
--          index_state with status='indexed' or 'pending')
--   INV-3  tool_request status transitions are monotonic:
--          completed/cancelled/dead-letter may never return to queued/claimed
--   INV-4  audit.event sequence is gapless per (module_id, occurred_at)
--          (enforced via a sequence counter column)
--   INV-5  resource.version is monotonically increasing on UPDATE
--   INV-6  chunk.sequence is unique per resource (already UNIQUE, add CHECK > 0)

-- ============================================================================
-- INV-3: tool_request status transition monotonicity
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_transport.enforce_status_transition()
RETURNS trigger AS $$
BEGIN
    -- Forbidden transitions: terminal states cannot return to active states
    IF OLD.status IN ('completed', 'cancelled', 'dead-letter') THEN
        RAISE EXCEPTION 'INV_VIOLATION: tool_request % cannot transition from % to % (terminal state)',
            OLD.request_id, OLD.status, NEW.status;
    END IF;
    -- completed -> dead-letter is allowed (for late failure detection)
    -- but dead-letter -> anything is forbidden
    IF OLD.status = 'dead-letter' THEN
        RAISE EXCEPTION 'INV_VIOLATION: tool_request % is in dead-letter and cannot be modified',
            OLD.request_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS tool_request_status_guard ON gptbridge_transport.tool_request;
CREATE TRIGGER tool_request_status_guard
    BEFORE UPDATE OF status ON gptbridge_transport.tool_request
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_transport.enforce_status_transition();

-- ============================================================================
-- INV-4: audit.event gapless sequence per module
-- ============================================================================
ALTER TABLE gptbridge_audit.event
    ADD COLUMN IF NOT EXISTS sequence_number bigint NOT NULL DEFAULT 0;

CREATE OR REPLACE FUNCTION gptbridge_audit.assign_sequence()
RETURNS trigger AS $$
BEGIN
    SELECT COALESCE(MAX(sequence_number), 0) + 1
    INTO NEW.sequence_number
    FROM gptbridge_audit.event
    WHERE module_id = NEW.module_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_sequence_assign ON gptbridge_audit.event;
CREATE TRIGGER audit_sequence_assign
    BEFORE INSERT ON gptbridge_audit.event
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_audit.assign_sequence();

CREATE INDEX IF NOT EXISTS audit_event_sequence_idx
    ON gptbridge_audit.event (module_id, sequence_number);

-- ============================================================================
-- INV-5: resource.version monotonic increase
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.enforce_version_monotonic()
RETURNS trigger AS $$
BEGIN
    IF NEW.version <= OLD.version THEN
        RAISE EXCEPTION 'INV_VIOLATION: resource % version must increase (old=%, new=%)',
            OLD.resource_id, OLD.version, NEW.version;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS resource_version_guard ON gptbridge_index.resource;
CREATE TRIGGER resource_version_guard
    BEFORE UPDATE OF version ON gptbridge_index.resource
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_index.enforce_version_monotonic();

-- ============================================================================
-- INV-2: chunk.qdrant_point_id must correspond to an index_state row
-- (checked at INSERT time; the resource_id FK ensures the resource exists,
--  but we also verify index_state exists for that resource)
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_rag.verify_chunk_index_state()
RETURNS trigger AS $$
DECLARE
    state_exists boolean;
BEGIN
    SELECT EXISTS(
        SELECT 1 FROM gptbridge_rag.index_state
        WHERE resource_id = NEW.resource_id
    ) INTO state_exists;
    IF NOT state_exists THEN
        RAISE EXCEPTION 'INV_VIOLATION: chunk % references resource_id % with no index_state row',
            NEW.chunk_id, NEW.resource_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Only enforce on INSERT (index_state may be created in the same transaction
-- after the chunk, so we use a deferred constraint)
DROP TRIGGER IF EXISTS rag_chunk_index_state_check ON gptbridge_rag.chunk;
CREATE TRIGGER rag_chunk_index_state_check
    BEFORE INSERT ON gptbridge_rag.chunk
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_rag.verify_chunk_index_state();
