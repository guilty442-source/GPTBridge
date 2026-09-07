-- 005_central_index_composite_indexes.sql
-- Composite indexes for the central resource index.
-- Optimizes the most common query patterns:
--   (module_id, resource_type, status) — module-scoped filtered listings
--   (logical_key, module_id)           — logical key lookups within a module
--   (locator_id)                       — opaque locator resolution
--   (updated_at)                       — reconciliation and incremental sync
-- A8/E21.

-- Replace the existing single-column index with a composite that covers
-- the most common filtered listing pattern.
DROP INDEX IF EXISTS gptbridge_index.resource_module_category_idx;
CREATE INDEX IF NOT EXISTS resource_module_type_status_idx
    ON gptbridge_index.resource (module_id, resource_type, index_status);

-- Logical key lookup within a module (A52/E38 resource registry contract)
CREATE INDEX IF NOT EXISTS resource_logical_key_module_idx
    ON gptbridge_index.resource (logical_key, module_id)
    WHERE logical_key IS NOT NULL AND logical_key <> '';

-- Locator resolution (opaque locator → resource)
CREATE INDEX IF NOT EXISTS resource_locator_idx
    ON gptbridge_index.resource (locator_id);

-- Reconciliation and incremental sync (most recently updated resources)
CREATE INDEX IF NOT EXISTS resource_updated_at_idx
    ON gptbridge_index.resource (updated_at);

-- Chunk lookup by (module_id, resource_id, version) for Qdrant rebuild
CREATE INDEX IF NOT EXISTS rag_chunk_module_resource_version_idx
    ON gptbridge_rag.chunk (module_id, resource_id, sequence);
