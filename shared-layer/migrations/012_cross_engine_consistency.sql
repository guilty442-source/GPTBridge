-- 012_cross_engine_consistency.sql
-- Cross-engine consistency contract: every resource across PostgreSQL,
-- SQLite, and Qdrant carries a unified identity tuple:
--   resource_id / revision / generation / hash
-- Any side whose version is behind is marked 'stale', never silently
-- overwritten.
--
-- A371-A374 + A8/E21 + A44/E30.

-- ============================================================================
-- Add generation and hash columns to the central resource table.
-- `backend_generation` increments on every major restore/rebuild so stale
-- connections are rejected (Generation Fence, item 14).
-- `content_hash` already exists; `revision` maps to the existing `version`
-- column.  We add `generation` as a separate monotonic counter that
-- increments only on structural rebuilds, not on every content update.
-- ============================================================================
ALTER TABLE gptbridge_index.resource
    ADD COLUMN IF NOT EXISTS backend_generation bigint NOT NULL DEFAULT 1;
ALTER TABLE gptbridge_index.resource
    ADD COLUMN IF NOT EXISTS stale boolean NOT NULL DEFAULT false;

-- Mark a resource as stale when its version falls behind the index_state
-- version (the index_state is the authoritative per-resource version).
CREATE OR REPLACE FUNCTION gptbridge_index.check_resource_stale()
RETURNS trigger AS $$
BEGIN
    -- If index_state version > resource version, mark stale
    PERFORM 1 FROM gptbridge_rag.index_state
    WHERE resource_id = NEW.resource_id
      AND version > NEW.version;
    IF FOUND THEN
        NEW.stale = true;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS resource_stale_check ON gptbridge_index.resource;
CREATE TRIGGER resource_stale_check
    BEFORE UPDATE ON gptbridge_index.resource
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_index.check_resource_stale();

-- ============================================================================
-- SQLite template: add matching columns so the contract is symmetric.
-- (The SQLite template is applied per-module; we update it separately.)
-- ============================================================================

-- ============================================================================
-- Qdrant consistency: index_state already has source_revision, content_hash,
-- backend_generation (from migration 009).  We add a trigger to mark
-- index_state as stale when its source_revision < resource.version.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_rag.check_index_state_stale()
RETURNS trigger AS $$
DECLARE
    resource_version bigint;
BEGIN
    SELECT version INTO resource_version
    FROM gptbridge_index.resource
    WHERE resource_id = NEW.resource_id;
    IF resource_version IS NOT NULL AND NEW.source_revision < resource_version THEN
        -- Mark as stale (status='stale' is a new status value)
        NEW.status = 'stale';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS index_state_stale_check ON gptbridge_rag.index_state;
CREATE TRIGGER index_state_stale_check
    BEFORE INSERT OR UPDATE ON gptbridge_rag.index_state
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_rag.check_index_state_stale();

-- ============================================================================
-- Cross-engine consistency view: shows all resources with their PG + Qdrant
-- version alignment status.
-- ============================================================================
CREATE OR REPLACE VIEW gptbridge_index.resource_consistency AS
SELECT
    r.resource_id,
    r.module_id,
    r.version AS pg_revision,
    r.content_hash AS pg_hash,
    r.backend_generation,
    r.stale AS pg_stale,
    COALESCE(s.source_revision, 0) AS qdrant_revision,
    COALESCE(s.content_hash, '') AS qdrant_hash,
    COALESCE(s.backend_generation, 1) AS qdrant_generation,
    CASE
        WHEN s.resource_id IS NULL THEN 'missing-qdrant'
        WHEN s.source_revision < r.version THEN 'qdrant-behind'
        WHEN s.source_revision > r.version THEN 'pg-behind'
        WHEN s.content_hash != COALESCE(r.content_hash, '') THEN 'hash-mismatch'
        ELSE 'in-sync'
    END AS consistency_status
FROM gptbridge_index.resource r
LEFT JOIN gptbridge_rag.index_state s ON s.resource_id = r.resource_id;

GRANT SELECT ON gptbridge_index.resource_consistency TO gptbridge_index_reader;
