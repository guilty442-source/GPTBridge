-- 019_authority_marker.sql
-- Authority Marker: every data record explicitly carries an authority_class
-- so degraded/cache/derived data is never mistaken for authoritative.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data; FORBID: role-substitution.
--   A44/E30 — four-functions-local; UNAVAILABLE:declare-closed-not-replace.
--   A10/E10 — explicit-allowlist; deny-by-default.
--
-- Authority classes:
--   central-official  — PostgreSQL central structured official data (A8).
--   module-private    — SQLite module-private operational state (A8).
--   derived           — computed/derived from other authoritative sources.
--   cache             — bounded cache of another authoritative source.
--   degraded-copy     — bounded degraded fallback (A44); NEVER authoritative.
--
-- This migration adds authority_class to:
--   * gptbridge_index.resource
--   * gptbridge_rag.index_state
--   * SQLite module template (resource_metadata)
-- Default is 'derived' (fail-safe: unknown data is never assumed authoritative).

-- ============================================================================
-- gptbridge_index.resource
-- ============================================================================
ALTER TABLE gptbridge_index.resource
    ADD COLUMN IF NOT EXISTS authority_class text NOT NULL DEFAULT 'derived'
    CHECK (authority_class IN (
        'central-official',
        'module-private',
        'derived',
        'cache',
        'degraded-copy'
    ));

-- Existing rows in the central index are central-official by definition.
UPDATE gptbridge_index.resource
SET authority_class = 'central-official'
WHERE authority_class = 'derived';

CREATE INDEX IF NOT EXISTS resource_authority_class_idx
    ON gptbridge_index.resource (authority_class, module_id);

-- ============================================================================
-- gptbridge_rag.index_state
-- ============================================================================
ALTER TABLE gptbridge_rag.index_state
    ADD COLUMN IF NOT EXISTS authority_class text NOT NULL DEFAULT 'derived'
    CHECK (authority_class IN (
        'central-official',
        'module-private',
        'derived',
        'cache',
        'degraded-copy'
    ));

-- Existing index_state rows are central-official (canonical RAG metadata).
UPDATE gptbridge_rag.index_state
SET authority_class = 'central-official'
WHERE authority_class = 'derived';

CREATE INDEX IF NOT EXISTS index_state_authority_class_idx
    ON gptbridge_rag.index_state (authority_class, status);

-- ============================================================================
-- Update the resource_lineage view to include authority_class.
-- (View is CREATE OR REPLACE, so we can extend it here.)
-- ============================================================================
CREATE OR REPLACE VIEW gptbridge_index.resource_lineage AS
SELECT
    r.resource_id,
    r.module_id,
    r.owner_id,
    r.resource_type,
    r.resource_label,
    r.classification,
    r.authority_class,
    r.version AS pg_revision,
    r.content_hash AS pg_hash,
    r.backend_generation,
    r.stale AS pg_stale,
    dl.source_module,
    dl.source_revision,
    dl.produce_method,
    dl.sync_path,
    dl.last_writer_id,
    dl.last_writer_at,
    dl.last_writer_actor_id,
    dl.last_writer_executor_id,
    dl.last_writer_decision_id,
    dl.last_writer_correlation_id,
    dl.lineage_metadata,
    loc.locator_id,
    loc.executor_type AS locator_executor_type,
    loc.location_key AS locator_location_key,
    loc.status AS locator_status,
    loc.physical_location IS NOT NULL AS has_physical_location,
    s.authority_class AS qdrant_authority_class,
    COALESCE(s.source_revision, 0) AS qdrant_revision,
    COALESCE(s.content_hash, '') AS qdrant_hash,
    COALESCE(s.status, 'missing') AS qdrant_index_status,
    CASE
        WHEN s.resource_id IS NULL THEN 'missing-qdrant'
        WHEN s.source_revision < r.version THEN 'qdrant-behind'
        WHEN s.source_revision > r.version THEN 'pg-behind'
        WHEN s.content_hash != COALESCE(r.content_hash, '') THEN 'hash-mismatch'
        ELSE 'in-sync'
    END AS qdrant_consistency
FROM gptbridge_index.resource r
LEFT JOIN gptbridge_index.data_lineage dl
    ON dl.resource_id = r.resource_id
LEFT JOIN registry.locations loc
    ON loc.resource_id = r.resource_id
LEFT JOIN gptbridge_rag.index_state s
    ON s.resource_id = r.resource_id;

GRANT SELECT ON gptbridge_index.resource_lineage TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_index.resource_lineage TO gptbridge_xingcheng_reader;
