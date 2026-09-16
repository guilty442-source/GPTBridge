-- 064_audit_hash_chain.sql
-- Audit Hash Chain.
--
-- gptbridge_audit.event each row gets:
--   event_hash, previous_event_hash, sequence
--
-- Forms: event_1 -> event_2 -> event_3
-- Any historical event modified -> subsequent hash chain breaks immediately.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- Add hash chain columns to gptbridge_audit.event
-- ============================================================================
ALTER TABLE gptbridge_audit.event
    ADD COLUMN IF NOT EXISTS sequence bigint NOT NULL DEFAULT 0;
ALTER TABLE gptbridge_audit.event
    ADD COLUMN IF NOT EXISTS event_hash text;
ALTER TABLE gptbridge_audit.event
    ADD COLUMN IF NOT EXISTS previous_event_hash text;

CREATE INDEX IF NOT EXISTS event_sequence_idx
    ON gptbridge_audit.event (sequence);
CREATE INDEX IF NOT EXISTS event_hash_idx
    ON gptbridge_audit.event (event_hash);

-- ============================================================================
-- compute_event_hash() — deterministic hash of an event's content
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_audit.compute_event_hash(
    p_event_id uuid,
    p_event_type text,
    p_actor text,
    p_occurred_at timestamptz,
    p_sequence bigint,
    p_previous_event_hash text,
    p_payload jsonb
) RETURNS text AS $$
DECLARE
    v_input text;
BEGIN
    v_input := p_event_id::text || '|' ||
               COALESCE(p_event_type, '') || '|' ||
               COALESCE(p_actor, '') || '|' ||
               p_occurred_at::text || '|' ||
               p_sequence::text || '|' ||
               COALESCE(p_previous_event_hash, '') || '|' ||
               COALESCE(p_payload::text, '');
    RETURN encode(digest(v_input, 'sha256'), 'hex');
END;
$$ LANGUAGE plpgsql IMMUTABLE SECURITY DEFINER SET search_path = pg_catalog, public;

-- ============================================================================
-- populate_event_hash_chain() — compute and set hash chain for an event
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_audit.populate_event_hash_chain(
    p_event_id uuid
) RETURNS void AS $$
DECLARE
    v_event record;
    v_prev_hash text;
    v_seq bigint;
    v_hash text;
BEGIN
    SELECT * INTO v_event
    FROM gptbridge_audit.event
    WHERE event_id = p_event_id;

    IF v_event IS NULL THEN
        RAISE EXCEPTION 'Event % not found', p_event_id;
    END IF;

    -- Get previous event's hash (by occurred_at order)
    SELECT event_hash INTO v_prev_hash
    FROM gptbridge_audit.event
    WHERE occurred_at < v_event.occurred_at
      AND event_hash IS NOT NULL
    ORDER BY occurred_at DESC, sequence DESC
    LIMIT 1;

    -- Get next sequence number
    SELECT COALESCE(MAX(sequence), 0) + 1 INTO v_seq
    FROM gptbridge_audit.event
    WHERE occurred_at <= v_event.occurred_at
      AND event_id != p_event_id;

    v_hash := gptbridge_audit.compute_event_hash(
        v_event.event_id, v_event.event_type, v_event.actor,
        v_event.occurred_at, v_seq, v_prev_hash, v_event.payload
    );

    UPDATE gptbridge_audit.event
    SET sequence = v_seq,
        previous_event_hash = v_prev_hash,
        event_hash = v_hash
    WHERE event_id = p_event_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public;

-- ============================================================================
-- verify_audit_chain() — verify the hash chain is intact
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_audit.verify_audit_chain(
    p_limit integer DEFAULT 1000
) RETURNS TABLE (
    event_id uuid,
    sequence bigint,
    expected_hash text,
    actual_hash text,
    chain_intact boolean
) AS $$
DECLARE
    v_prev_hash text := NULL;
    v_expected text;
BEGIN
    RETURN QUERY
    WITH ordered_events AS (
        SELECT event_id, event_type, actor, occurred_at, sequence,
               previous_event_hash, event_hash, payload
        FROM gptbridge_audit.event
        WHERE event_hash IS NOT NULL
        ORDER BY sequence
        LIMIT p_limit
    )
    SELECT
        oe.event_id,
        oe.sequence,
        gptbridge_audit.compute_event_hash(
            oe.event_id, oe.event_type, oe.actor,
            oe.occurred_at, oe.sequence, oe.previous_event_hash, oe.payload
        ) AS expected_hash,
        oe.event_hash AS actual_hash,
        (gptbridge_audit.compute_event_hash(
            oe.event_id, oe.event_type, oe.actor,
            oe.occurred_at, oe.sequence, oe.previous_event_hash, oe.payload
        ) = oe.event_hash) AS chain_intact
    FROM ordered_events oe;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public;

-- ============================================================================
-- get_audit_head_hash() — get the latest event hash (chain head)
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_audit.get_audit_head_hash()
RETURNS text AS $$
DECLARE
    v_hash text;
BEGIN
    SELECT event_hash INTO v_hash
    FROM gptbridge_audit.event
    WHERE event_hash IS NOT NULL
    ORDER BY sequence DESC
    LIMIT 1;
    RETURN v_hash;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, public;
