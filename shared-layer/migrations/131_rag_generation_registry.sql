-- 131_rag_generation_registry.sql
-- RAG-11 canonical generation registry.
--
-- ``gptbridge_rag.generation`` records the authoritative RAG generation
-- lifecycle (BUILDING -> VERIFYING -> ACTIVE -> RETIRED / FAILED) consumed by
-- ``core_system.rag.rag_metadata_queue`` (upsert_generation / generation
-- reads).  Previously the table was created only by the runtime bootstrap
-- DDL, which the least-privilege runtime role (``gptbridge_runtime``) cannot
-- execute; the migration chain is the sole schema evolution authority
-- (A515/A516), so the object is materialised here and the runtime degrades to
-- verify-only.
--
-- Shape mirrors ``core_system.rag.generation._GENERATION_DDL`` exactly.

CREATE TABLE IF NOT EXISTS gptbridge_rag.generation (
    generation_id TEXT PRIMARY KEY,
    index_schema_version TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL,
    chunk_policy_version TEXT NOT NULL,
    chunk_size INTEGER NOT NULL,
    chunk_overlap INTEGER NOT NULL,
    created_at_utc TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN
        ('BUILDING','VERIFYING','ACTIVE','RETIRED','FAILED')),
    collection_name TEXT NOT NULL,
    alias_name TEXT NOT NULL,
    previous_generation_id TEXT,
    points_count INTEGER DEFAULT 0,
    verification_result JSONB,
    error_message TEXT,
    parser_version TEXT DEFAULT 'v1',
    vector_schema_version TEXT DEFAULT 'v1',
    metadata_schema_version TEXT DEFAULT 'v1',
    policy_version TEXT DEFAULT 'v1',
    activated_at_utc TEXT,
    retired_at_utc TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_rag_generation_active
    ON gptbridge_rag.generation (alias_name) WHERE state = 'ACTIVE';

CREATE INDEX IF NOT EXISTS idx_rag_generation_alias_state
    ON gptbridge_rag.generation (alias_name, state);

-- Global registry (no module_id column): read-open to gptbridge principals,
-- write restricted to gptbridge roles — mirrors the ragpolicy RLS pattern
-- from migration 088.
ALTER TABLE gptbridge_rag.generation ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_rag.generation FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS rag_generation_select ON gptbridge_rag.generation;
CREATE POLICY rag_generation_select ON gptbridge_rag.generation
    FOR SELECT USING (true);

DROP POLICY IF EXISTS rag_generation_write ON gptbridge_rag.generation;
CREATE POLICY rag_generation_write ON gptbridge_rag.generation
    FOR ALL USING (current_user LIKE 'gptbridge_%')
    WITH CHECK (current_user LIKE 'gptbridge_%');

GRANT SELECT ON gptbridge_rag.generation TO gptbridge_index_reader;
GRANT SELECT ON gptbridge_rag.generation TO gptbridge_xingcheng_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_rag.generation
    TO gptbridge_index_executor;

-- Least-privilege runtime login needs generation upsert/read for the RAG-11
-- lifecycle; idempotent guarded grant (same pattern as migration 129).
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_runtime') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON gptbridge_rag.generation TO gptbridge_runtime';
    END IF;
END
$$;
