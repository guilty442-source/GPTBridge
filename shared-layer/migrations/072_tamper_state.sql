-- 072_tamper_state.sql
-- Tamper State.
--
-- Unified tamper state across all integrity checks:
--   verified, unverified, mismatch, tampered, incomplete, rebuild_required
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- tamper_state_registry — central registry of tamper states
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.tamper_state_registry (
    state_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type text NOT NULL CHECK (entity_type IN (
        'audit_chain', 'resource_hash', 'sqlite_digest',
        'qdrant_integrity', 'merkle_root', 'integrity_snapshot',
        'restore_verification', 'reconcile_batch', 'schema_contract',
        'governance_codex'
    )),
    entity_id text NOT NULL,
    tamper_state text NOT NULL DEFAULT 'unverified' CHECK (tamper_state IN (
        'verified', 'unverified', 'mismatch', 'tampered',
        'incomplete', 'rebuild_required'
    )),
    detected_at timestamptz NOT NULL DEFAULT now(),
    detected_by text NOT NULL,
    details jsonb,
    resolved_at timestamptz,
    resolved_by text,
    resolution_notes text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.tamper_state_registry ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.tamper_state_registry FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS tamper_state_read ON gptbridge_index.tamper_state_registry;
CREATE POLICY tamper_state_read ON gptbridge_index.tamper_state_registry
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS tamper_state_write ON gptbridge_index.tamper_state_registry;
CREATE POLICY tamper_state_write ON gptbridge_index.tamper_state_registry
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.tamper_state_registry FROM PUBLIC;
GRANT SELECT ON gptbridge_index.tamper_state_registry TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.tamper_state_registry
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS tamper_state_entity_idx
    ON gptbridge_index.tamper_state_registry (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS tamper_state_state_idx
    ON gptbridge_index.tamper_state_registry (tamper_state, detected_at);

-- ============================================================================
-- record_tamper_state() — record a tamper state for an entity
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_tamper_state(
    p_entity_type text,
    p_entity_id text,
    p_tamper_state text,
    p_detected_by text,
    p_details jsonb DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.tamper_state_registry (
        entity_type, entity_id, tamper_state, detected_by, details
    )
    VALUES (
        p_entity_type, p_entity_id, p_tamper_state, p_detected_by, p_details
    )
    RETURNING state_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- resolve_tamper_state() — resolve a tamper state
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.resolve_tamper_state(
    p_state_id uuid,
    p_resolved_by text,
    p_resolution_notes text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.tamper_state_registry
    SET resolved_at = now(),
        resolved_by = p_resolved_by,
        resolution_notes = p_resolution_notes,
        tamper_state = 'verified',
        updated_at = now()
    WHERE state_id = p_state_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_active_tamper_issues() — find unresolved tamper issues
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_active_tamper_issues(
    p_limit integer DEFAULT 100
) RETURNS TABLE (
    state_id uuid,
    entity_type text,
    entity_id text,
    tamper_state text,
    detected_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT state_id, entity_type, entity_id, tamper_state, detected_at
    FROM gptbridge_index.tamper_state_registry
    WHERE resolved_at IS NULL
      AND tamper_state IN ('mismatch', 'tampered', 'incomplete', 'rebuild_required')
    ORDER BY detected_at DESC
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
