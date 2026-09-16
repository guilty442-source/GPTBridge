-- 027_two_stage_deletion.sql
-- Two-Stage Deletion: tombstone → retention window → purge, with
-- cross-engine sync (PostgreSQL index, SQLite private data, Qdrant vectors).
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A44/E30 — four-functions-local.
--   A46/E22 — Audit: mandatory-ledger.
--   A52/E38 — RAG: Qdrant canonical semantic index.
--
-- Deletion stages:
--   active       — normal state
--   tombstone    — marked for deletion, still recoverable
--   retention    — in retention window, pending purge
--   purged       — fully deleted from all engines
--
-- This migration adds:
--   * deletion_stage / tombstoned_at / purge_after columns to resource
--   * A trigger that sets purge_after = tombstoned_at + retention period
--   * A function to advance stages and check for purge eligibility

-- ============================================================================
-- resource — deletion stage columns
-- ============================================================================
ALTER TABLE gptbridge_index.resource
    ADD COLUMN IF NOT EXISTS deletion_stage text NOT NULL DEFAULT 'active'
    CHECK (deletion_stage IN ('active', 'tombstone', 'retention', 'purged')),
    ADD COLUMN IF NOT EXISTS tombstoned_at timestamptz,
    ADD COLUMN IF NOT EXISTS purge_after timestamptz;

CREATE INDEX IF NOT EXISTS resource_deletion_stage_idx
    ON gptbridge_index.resource (deletion_stage, purge_after)
    WHERE deletion_stage IN ('tombstone', 'retention');

-- ============================================================================
-- index_state — deletion stage (sync with resource)
-- ============================================================================
ALTER TABLE gptbridge_rag.index_state
    ADD COLUMN IF NOT EXISTS deletion_stage text NOT NULL DEFAULT 'active'
    CHECK (deletion_stage IN ('active', 'tombstone', 'retention', 'purged'));

-- ============================================================================
-- tombstone_resource() — mark a resource as tombstoned.
-- Sets deletion_stage='tombstone', tombstoned_at=now(), and
-- purge_after = tombstoned_at + retention_days.
-- The runtime deletion coordinator calls this, then syncs the tombstone
-- to SQLite (pending-delete) and Qdrant (point tombstone).
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.tombstone_resource(
    p_resource_id text,
    p_retention_days integer DEFAULT 30
) RETURNS void AS $$
DECLARE
    v_module_id text;
BEGIN
    SELECT module_id INTO v_module_id
    FROM gptbridge_index.resource WHERE resource_id = p_resource_id;

    IF v_module_id IS NULL THEN
        RAISE EXCEPTION 'TOMBSTONE: resource % not found', p_resource_id;
    END IF;

    UPDATE gptbridge_index.resource
    SET deletion_stage = 'tombstone',
        tombstoned_at = now(),
        purge_after = now() + (p_retention_days || ' days')::interval,
        updated_at = now()
    WHERE resource_id = p_resource_id;

    -- Sync tombstone to index_state
    UPDATE gptbridge_rag.index_state
    SET deletion_stage = 'tombstone',
        updated_at = now()
    WHERE resource_id = p_resource_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index, gptbridge_rag;

-- ============================================================================
-- advance_deletion_stage() — advance tombstone → retention → purged.
-- Called by the runtime deletion coordinator after each stage completes.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.advance_deletion_stage(
    p_resource_id text
) RETURNS text AS $$
DECLARE
    v_stage text;
    v_purge_after timestamptz;
BEGIN
    SELECT deletion_stage, purge_after INTO v_stage, v_purge_after
    FROM gptbridge_index.resource WHERE resource_id = p_resource_id;

    IF v_stage IS NULL THEN
        RETURN 'not-found';
    END IF;

    IF v_stage = 'tombstone' AND v_purge_after IS NOT NULL AND now() >= v_purge_after THEN
        -- Tombstone → retention (retention window expired, ready to purge)
        UPDATE gptbridge_index.resource
        SET deletion_stage = 'retention', updated_at = now()
        WHERE resource_id = p_resource_id;
        UPDATE gptbridge_rag.index_state
        SET deletion_stage = 'retention', updated_at = now()
        WHERE resource_id = p_resource_id;
        RETURN 'retention';
    ELSIF v_stage = 'retention' THEN
        -- Retention → purged (all engines confirmed deleted)
        UPDATE gptbridge_index.resource
        SET deletion_stage = 'purged', updated_at = now()
        WHERE resource_id = p_resource_id;
        UPDATE gptbridge_rag.index_state
        SET deletion_stage = 'purged', updated_at = now()
        WHERE resource_id = p_resource_id;
        RETURN 'purged';
    END IF;

    RETURN v_stage;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index, gptbridge_rag;

-- ============================================================================
-- get_purge_eligible() — list resources ready to advance from tombstone.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_purge_eligible(
    p_limit integer DEFAULT 100
) RETURNS TABLE (
    resource_id text,
    module_id text,
    tombstoned_at timestamptz,
    purge_after timestamptz
) AS $$
    SELECT resource_id, module_id, tombstoned_at, purge_after
    FROM gptbridge_index.resource
    WHERE deletion_stage = 'tombstone'
      AND purge_after IS NOT NULL
      AND now() >= purge_after
    ORDER BY purge_after
    LIMIT p_limit;
$$ LANGUAGE sql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
