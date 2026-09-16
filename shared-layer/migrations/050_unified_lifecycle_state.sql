-- 050_unified_lifecycle_state.sql
-- Unified Data Lifecycle State Machine.
--
-- All long-lived data uses a unified state:
--   ACTIVE → STALE → SUPERSEDED → TOMBSTONED → ARCHIVED → PURGED
--
-- Rules:
--   ACTIVE     — normal use
--   STALE      — expired but may still be referenced
--   SUPERSEDED — replaced by a newer version
--   TOMBSTONED — logical deletion, awaiting cleanup
--   ARCHIVED   — moved out of hot data area
--   PURGED     — permanently removed
--
-- No direct DELETE.  All transitions go through the state machine.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- lifecycle_state — the canonical state machine
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.lifecycle_state (
    entity_type text NOT NULL,  -- 'resource', 'transport', 'audit', 'rag', 'sqlite'
    entity_id text NOT NULL,    -- resource_id, request_id, event_id, etc.
    lifecycle_state text NOT NULL CHECK (lifecycle_state IN (
        'ACTIVE', 'STALE', 'SUPERSEDED', 'TOMBSTONED', 'ARCHIVED', 'PURGED'
    )),
    previous_state text,
    reason text,
    transitioned_at timestamptz NOT NULL DEFAULT now(),
    transitioned_by text NOT NULL,
    PRIMARY KEY (entity_type, entity_id)
);

ALTER TABLE gptbridge_index.lifecycle_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.lifecycle_state FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS lifecycle_state_read ON gptbridge_index.lifecycle_state;
CREATE POLICY lifecycle_state_read ON gptbridge_index.lifecycle_state
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS lifecycle_state_write ON gptbridge_index.lifecycle_state;
CREATE POLICY lifecycle_state_write ON gptbridge_index.lifecycle_state
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.lifecycle_state FROM PUBLIC;
GRANT SELECT ON gptbridge_index.lifecycle_state TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.lifecycle_state
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS lifecycle_state_idx
    ON gptbridge_index.lifecycle_state (lifecycle_state, entity_type);
CREATE INDEX IF NOT EXISTS lifecycle_state_transitioned_idx
    ON gptbridge_index.lifecycle_state (transitioned_at);

-- ============================================================================
-- transition_lifecycle_state() — enforce the state machine
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.transition_lifecycle_state(
    p_entity_type text,
    p_entity_id text,
    p_new_state text,
    p_transitioned_by text,
    p_reason text DEFAULT NULL
) RETURNS void AS $$
DECLARE
    v_current text;
    v_valid_transitions text[];
    v_allowed text[];
BEGIN
    SELECT lifecycle_state INTO v_current
    FROM gptbridge_index.lifecycle_state
    WHERE entity_type = p_entity_type AND entity_id = p_entity_id;

    -- First entry is always ACTIVE
    IF v_current IS NULL THEN
        IF p_new_state != 'ACTIVE' THEN
            RAISE EXCEPTION 'Initial lifecycle state must be ACTIVE, got %', p_new_state;
        END IF;
        INSERT INTO gptbridge_index.lifecycle_state (
            entity_type, entity_id, lifecycle_state, reason, transitioned_by
        )
        VALUES (p_entity_type, p_entity_id, p_new_state, p_reason, p_transitioned_by);
        RETURN;
    END IF;

    -- Define valid transitions
    v_valid_transitions := ARRAY[
        'ACTIVE:STALE',
        'ACTIVE:SUPERSEDED',
        'ACTIVE:TOMBSTONED',
        'STALE:SUPERSEDED',
        'STALE:TOMBSTONED',
        'SUPERSEDED:TOMBSTONED',
        'TOMBSTONED:ARCHIVED',
        'ARCHIVED:PURGED',
        'TOMBSTONED:PURGED'  -- direct purge for non-archivable
    ];

    IF NOT (v_current || ':' || p_new_state = ANY(v_valid_transitions)) THEN
        RAISE EXCEPTION 'Invalid lifecycle transition: % -> %', v_current, p_new_state;
    END IF;

    UPDATE gptbridge_index.lifecycle_state
    SET lifecycle_state = p_new_state,
        previous_state = v_current,
        reason = p_reason,
        transitioned_at = now(),
        transitioned_by = p_transitioned_by
    WHERE entity_type = p_entity_type AND entity_id = p_entity_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_lifecycle_state() — get the current lifecycle state of an entity
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_lifecycle_state(
    p_entity_type text,
    p_entity_id text
) RETURNS text AS $$
DECLARE
    v_state text;
BEGIN
    SELECT lifecycle_state INTO v_state
    FROM gptbridge_index.lifecycle_state
    WHERE entity_type = p_entity_type AND entity_id = p_entity_id;
    RETURN COALESCE(v_state, 'ACTIVE');
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_entities_by_state() — list entities in a given state
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_entities_by_state(
    p_entity_type text,
    p_state text,
    p_limit integer DEFAULT 100
) RETURNS TABLE (
    entity_id text,
    transitioned_at timestamptz,
    reason text
) AS $$
BEGIN
    RETURN QUERY
    SELECT entity_id, transitioned_at, reason
    FROM gptbridge_index.lifecycle_state
    WHERE entity_type = p_entity_type AND lifecycle_state = p_state
    ORDER BY transitioned_at
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
