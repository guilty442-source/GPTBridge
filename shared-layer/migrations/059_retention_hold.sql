-- 059_retention_hold.sql
-- Legal / Governance Hold.
--
-- Some data cannot be deleted even after retention expires:
--   audit investigation, governance review, repair investigation,
--   identity dispute
--
-- When a hold is active:
--   TOMBSTONED → cannot PURGE
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- retention_hold — legal/governance hold on entities
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.retention_hold (
    hold_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type text NOT NULL,  -- 'resource', 'transport', 'audit', 'rag'
    entity_id text NOT NULL,
    hold_reason text NOT NULL CHECK (hold_reason IN (
        'audit_investigation', 'governance_review', 'repair_investigation',
        'identity_dispute', 'legal_hold', 'compliance_hold'
    )),
    hold_description text,
    hold_active boolean NOT NULL DEFAULT true,
    placed_by text NOT NULL,
    placed_at timestamptz NOT NULL DEFAULT now(),
    released_by text,
    released_at timestamptz,
    expected_release_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.retention_hold ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.retention_hold FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS retention_hold_read ON gptbridge_index.retention_hold;
CREATE POLICY retention_hold_read ON gptbridge_index.retention_hold
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS retention_hold_write ON gptbridge_index.retention_hold;
CREATE POLICY retention_hold_write ON gptbridge_index.retention_hold
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.retention_hold FROM PUBLIC;
GRANT SELECT ON gptbridge_index.retention_hold TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.retention_hold
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS retention_hold_entity_idx
    ON gptbridge_index.retention_hold (entity_type, entity_id) WHERE hold_active = true;
CREATE INDEX IF NOT EXISTS retention_hold_active_idx
    ON gptbridge_index.retention_hold (hold_active, placed_at);

-- ============================================================================
-- place_hold() — place a retention hold on an entity
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.place_hold(
    p_entity_type text,
    p_entity_id text,
    p_hold_reason text,
    p_placed_by text,
    p_description text DEFAULT NULL,
    p_expected_release_at timestamptz DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.retention_hold (
        entity_type, entity_id, hold_reason, hold_description,
        placed_by, expected_release_at
    )
    VALUES (
        p_entity_type, p_entity_id, p_hold_reason, p_description,
        p_placed_by, p_expected_release_at
    )
    RETURNING hold_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- release_hold() — release a retention hold
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.release_hold(
    p_hold_id uuid,
    p_released_by text
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.retention_hold
    SET hold_active = false,
        released_by = p_released_by,
        released_at = now(),
        updated_at = now()
    WHERE hold_id = p_hold_id AND hold_active = true;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- has_active_hold() — check if an entity has an active hold
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.has_active_hold(
    p_entity_type text,
    p_entity_id text
) RETURNS boolean AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM gptbridge_index.retention_hold
        WHERE entity_type = p_entity_type
          AND entity_id = p_entity_id
          AND hold_active = true
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
