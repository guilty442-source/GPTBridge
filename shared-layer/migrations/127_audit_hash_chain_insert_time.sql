-- 127_audit_hash_chain_insert_time.sql
-- Audit hash chain: compute sequence + previous_event_hash + event_hash at
-- INSERT time (append-only compatible).
--
-- Fixes 064_audit_hash_chain.sql:
--   * 064 assumed columns (event_type, actor, payload) that do not exist on
--     gptbridge_audit.event (real columns: actor_id, module_id, resource_id,
--     action, outcome, decision_id, details, occurred_at);
--   * 064 populated the chain via UPDATE, which the 006 append-only trigger
--     forbids (AUDIT_APPEND_ONLY) — the chain could never be built.
--
-- Design: a BEFORE INSERT trigger computes the chain from the NEW row, and a
-- transaction advisory lock serializes concurrent appends so sequence and
-- previous hash are deterministic (A449 durability).
--
-- Codex basis:
--   A46/E22   — Audit: mandatory-ledger;
--   A448      — audit event machine schema;
--   A449/A451 — durable serialized audit append before success.

-- ============================================================================
-- Canonical hash over the real event columns
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_audit.compute_event_hash(
    p_event_id uuid,
    p_actor_id text,
    p_module_id text,
    p_action text,
    p_outcome text,
    p_decision_id text,
    p_occurred_at timestamptz,
    p_sequence bigint,
    p_previous_event_hash text,
    p_details jsonb
) RETURNS text AS $$
DECLARE
    v_input text;
BEGIN
    v_input := p_event_id::text || '|' ||
               COALESCE(p_actor_id, '') || '|' ||
               COALESCE(p_module_id, '') || '|' ||
               COALESCE(p_action, '') || '|' ||
               COALESCE(p_outcome, '') || '|' ||
               COALESCE(p_decision_id, '') || '|' ||
               p_occurred_at::text || '|' ||
               p_sequence::text || '|' ||
               COALESCE(p_previous_event_hash, '') || '|' ||
               COALESCE(p_details::text, '');
    RETURN encode(digest(v_input, 'sha256'), 'hex');
END;
$$ LANGUAGE plpgsql IMMUTABLE SECURITY DEFINER SET search_path = pg_catalog, public;

-- Retire the 064 UPDATE-based chain population: it must never run again on an
-- append-only ledger.
CREATE OR REPLACE FUNCTION gptbridge_audit.populate_event_hash_chain(
    p_event_id uuid
) RETURNS void AS $$
BEGIN
    RAISE EXCEPTION
        'AUDIT_CHAIN_IS_INSERT_TIME: the hash chain is computed by the '
        'audit_hash_chain_insert trigger; UPDATE-based population is '
        'forbidden on an append-only ledger (A46/A449)';
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, public;

-- ============================================================================
-- BEFORE INSERT trigger: sequence + previous hash + event hash
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_audit.set_event_hash_chain()
RETURNS trigger AS $$
DECLARE
    v_prev_hash text;
    v_seq bigint;
BEGIN
    -- Serialize concurrent appends so the chain has no duplicate sequence and
    -- no torn previous-hash link (A449 durability).
    PERFORM pg_advisory_xact_lock(hashtext('gptbridge_audit.event.chain'));

    SELECT event_hash, sequence
      INTO v_prev_hash, v_seq
      FROM gptbridge_audit.event
     ORDER BY sequence DESC, occurred_at DESC
     LIMIT 1;

    NEW.sequence := COALESCE(v_seq, 0) + 1;
    NEW.previous_event_hash := v_prev_hash;
    NEW.event_hash := gptbridge_audit.compute_event_hash(
        NEW.event_id,
        NEW.actor_id,
        NEW.module_id,
        NEW.action,
        NEW.outcome,
        NEW.decision_id,
        NEW.occurred_at,
        NEW.sequence,
        NEW.previous_event_hash,
        NEW.details
    );
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_audit;

DROP TRIGGER IF EXISTS audit_hash_chain_insert ON gptbridge_audit.event;
CREATE TRIGGER audit_hash_chain_insert
    BEFORE INSERT ON gptbridge_audit.event
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_audit.set_event_hash_chain();

-- ============================================================================
-- Backfill: assign sequence + hashes to pre-existing rows in occurrence order.
-- Runs once with the append-only UPDATE trigger explicitly suspended and a
-- temporary owner-only update policy, then restores the append-only state —
-- history becomes verifiable without ever deleting rows.
-- ============================================================================
ALTER TABLE gptbridge_audit.event DISABLE TRIGGER audit_no_update;

DROP POLICY IF EXISTS event_hash_backfill ON gptbridge_audit.event;
CREATE POLICY event_hash_backfill ON gptbridge_audit.event
    FOR UPDATE USING (true) WITH CHECK (true);

DO $$
DECLARE
    v_row record;
    v_prev text := NULL;
    v_seq bigint := 0;
BEGIN
    FOR v_row IN
        SELECT * FROM gptbridge_audit.event
         WHERE event_hash IS NULL
         ORDER BY occurred_at, event_id
    LOOP
        v_seq := v_seq + 1;
        UPDATE gptbridge_audit.event
           SET sequence = v_seq,
               previous_event_hash = v_prev,
               event_hash = gptbridge_audit.compute_event_hash(
                   v_row.event_id, v_row.actor_id, v_row.module_id,
                   v_row.action, v_row.outcome, v_row.decision_id,
                   v_row.occurred_at, v_seq, v_prev, v_row.details)
         WHERE event_id = v_row.event_id;
        SELECT event_hash INTO v_prev
          FROM gptbridge_audit.event
         WHERE event_id = v_row.event_id;
    END LOOP;
END;
$$;

DROP POLICY IF EXISTS event_hash_backfill ON gptbridge_audit.event;
ALTER TABLE gptbridge_audit.event ENABLE TRIGGER audit_no_update;

-- ============================================================================
-- Archival without DELETE: append-only tables archive by partition detach.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_audit.archive_audit_events(
    p_older_than_days integer DEFAULT 30,
    p_batch_limit integer DEFAULT 1000
) RETURNS integer AS $$
DECLARE
    v_partition text;
    v_count integer := 0;
BEGIN
    FOR v_partition IN
        SELECT child.relname
          FROM pg_inherits
          JOIN pg_class parent ON pg_inherits.inhparent = parent.oid
          JOIN pg_namespace pn ON parent.relnamespace = pn.oid
          JOIN pg_class child ON pg_inherits.inhrelid = child.oid
         WHERE pn.nspname = 'gptbridge_audit'
           AND parent.relname = 'event'
    LOOP
        EXECUTE format(
            'ALTER TABLE gptbridge_audit.event DETACH PARTITION gptbridge_audit.%I',
            v_partition
        );
        v_count := v_count + 1;
    END LOOP;
    IF v_count = 0 THEN
        RAISE EXCEPTION
            'AUDIT_ARCHIVE_REQUIRES_PARTITION: gptbridge_audit.event is not '
            'partitioned; an append-only ledger is archived by partition '
            'swap, never by DELETE (A46/A449)';
    END IF;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, gptbridge_audit, gptbridge_security;
