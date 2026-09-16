"""PostgreSQL Outbox Schema — 正規化的 outbox_event 表定義。

這是 canonical 寫入流程的核心部分，不是錯誤紀錄。
"""

from __future__ import annotations

OUTBOX_TABLE_SQL = """
-- Outbox event table for transactional RAG indexing
-- This is part of the canonical write path, not an error log
CREATE TABLE IF NOT EXISTS gptbridge_rag.outbox_event (
    event_id UUID PRIMARY KEY,
    request_id TEXT NOT NULL,

    operation TEXT NOT NULL CHECK (operation IN ('UPSERT','DELETE','REINDEX','UPDATE_METADATA')),
    module_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,

    source_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    generation_id TEXT NOT NULL,

    payload JSONB,

    state TEXT NOT NULL CHECK (state IN ('PENDING','PROCESSING','SUCCEEDED','RETRY','DEAD_LETTER')),
    attempt_count INTEGER DEFAULT 0,
    next_retry_at TIMESTAMPTZ,
    last_error TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

-- Indexes for worker efficiency
CREATE INDEX IF NOT EXISTS idx_outbox_event_state_created
ON gptbridge_rag.outbox_event (state, created_at);

CREATE INDEX IF NOT EXISTS idx_outbox_event_generation
ON gptbridge_rag.outbox_event (generation_id, state);

CREATE INDEX IF NOT EXISTS idx_outbox_event_retry
ON gptbridge_rag.outbox_event (next_retry_at)
WHERE state = 'RETRY';

-- Updated at trigger
CREATE OR REPLACE FUNCTION gptbridge_rag.update_outbox_updated_at()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_outbox_event_updated_at ON gptbridge_rag.outbox_event;
CREATE TRIGGER trg_outbox_event_updated_at
    BEFORE UPDATE ON gptbridge_rag.outbox_event
    FOR EACH ROW EXECUTE FUNCTION gptbridge_rag.update_outbox_updated_at();

-- Optional: partition by generation_id for large scale
-- CREATE TABLE gptbridge_rag.outbox_event (
--     ... same columns ...
-- ) PARTITION BY HASH (generation_id);
"""

INDEX_STATE_TABLE_SQL = """
-- Authoritative index state per resource/chunk
CREATE TABLE IF NOT EXISTS gptbridge_rag.index_state (
    resource_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    chunk_id TEXT NOT NULL,
    point_id TEXT NOT NULL,

    generation_id TEXT NOT NULL,
    source_version INTEGER NOT NULL,
    content_hash TEXT NOT NULL,

    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL,
    chunk_policy_version TEXT NOT NULL,
    chunk_size INTEGER NOT NULL,
    chunk_overlap INTEGER NOT NULL,
    indexed_at_utc TIMESTAMPTZ NOT NULL,

    state TEXT NOT NULL CHECK (state IN (
        'SOURCE','PARSED','CHUNKED','EMBEDDED',
        'INDEX_PENDING','CANONICAL_INDEXED','ACTIVE',
        'STALE','TOMBSTONED','DEGRADED_PENDING','RECONCILING'
    )),

    knowledge_kind TEXT NOT NULL DEFAULT 'SOURCE',
    rag_types TEXT[] NOT NULL DEFAULT '{}',
    data_category TEXT NOT NULL DEFAULT 'internal',
    resource_type TEXT NOT NULL DEFAULT 'document',

    qdrant_collection TEXT,
    qdrant_point_id TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (resource_id, module_id, chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_index_state_generation
ON gptbridge_rag.index_state (generation_id, state);

CREATE INDEX IF NOT EXISTS idx_index_state_lookup
ON gptbridge_rag.index_state (module_id, resource_id, state);

CREATE INDEX IF NOT EXISTS idx_index_state_content_hash
ON gptbridge_rag.index_state (content_hash);
"""

RESOURCE_VERSION_TABLE_SQL = """
-- Optimistic locking resource versions
CREATE TABLE IF NOT EXISTS gptbridge_rag.resource_versions (
    resource_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    version INTEGER NOT NULL,

    content_hash TEXT NOT NULL,
    generation_id TEXT NOT NULL,

    knowledge_kind TEXT NOT NULL DEFAULT 'SOURCE',
    state TEXT NOT NULL DEFAULT 'SOURCE',

    chunk_policy_version TEXT NOT NULL,
    chunk_count INTEGER NOT NULL DEFAULT 0,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL,

    source_version INTEGER,
    previous_version INTEGER,

    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    PRIMARY KEY (resource_id, module_id, version)
);

CREATE INDEX IF NOT EXISTS idx_resource_versions_latest
ON gptbridge_rag.resource_versions (resource_id, module_id, version DESC);

-- Current version view
CREATE OR REPLACE VIEW gptbridge_rag.current_resource_versions AS
SELECT DISTINCT ON (resource_id, module_id)
    resource_id, module_id, version, content_hash, generation_id,
    knowledge_kind, state, chunk_policy_version, chunk_count,
    embedding_model, embedding_dimension, updated_at, created_at,
    previous_version, source_version
FROM gptbridge_rag.resource_versions
ORDER BY resource_id, module_id, version DESC;
"""

CHUNK_TABLE_SQL = """
-- Stable chunks with deterministic IDs
CREATE TABLE IF NOT EXISTS gptbridge_rag.chunks (
    chunk_id TEXT PRIMARY KEY,
    resource_id TEXT NOT NULL,
    module_id TEXT NOT NULL,
    generation_id TEXT NOT NULL,

    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    character_start INTEGER NOT NULL,
    character_end INTEGER NOT NULL,
    chunk_policy_version TEXT NOT NULL,

    version INTEGER NOT NULL DEFAULT 1,
    state TEXT NOT NULL DEFAULT 'CHUNKED',

    qdrant_point_id TEXT,
    embedding_cached BOOLEAN DEFAULT FALSE,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_chunks_resource
ON gptbridge_rag.chunks (resource_id, module_id, generation_id);

CREATE INDEX IF NOT EXISTS idx_chunks_content_hash
ON gptbridge_rag.chunks (content_hash);

CREATE INDEX IF NOT EXISTS idx_chunks_state
ON gptbridge_rag.chunks (state, generation_id);
"""

PROVENANCE_TABLE_SQL = """
-- Derived knowledge provenance tracking
CREATE TABLE IF NOT EXISTS gptbridge_rag.provenance (
    derived_resource_id TEXT NOT NULL,
    derived_chunk_id TEXT NOT NULL,
    source_resource_id TEXT NOT NULL,
    source_chunk_id TEXT NOT NULL,
    source_generation_id TEXT NOT NULL,

    generation_model TEXT NOT NULL,
    generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content_hash TEXT NOT NULL,

    PRIMARY KEY (derived_resource_id, derived_chunk_id, source_resource_id, source_chunk_id)
);

CREATE INDEX IF NOT EXISTS idx_provenance_source
ON gptbridge_rag.provenance (source_resource_id, source_chunk_id);

CREATE INDEX IF NOT EXISTS idx_provenance_derived
ON gptbridge_rag.provenance (derived_resource_id, derived_chunk_id);
"""

EMBEDDING_CACHE_TABLE_SQL = """
-- Embedding cache for reusability
CREATE TABLE IF NOT EXISTS gptbridge_rag.embedding_cache (
    cache_id UUID PRIMARY KEY,
    content_hash TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL,
    vector BYTEA NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    hit_count INTEGER DEFAULT 0,

    UNIQUE (content_hash, embedding_model, embedding_dimension)
);

CREATE INDEX IF NOT EXISTS idx_embedding_cache_lookup
ON gptbridge_rag.embedding_cache (content_hash, embedding_model, embedding_dimension);
"""

GENERATION_TABLE_SQL = """
-- Index generation lifecycle
CREATE TABLE IF NOT EXISTS gptbridge_rag.generations (
    generation_id TEXT PRIMARY KEY,
    index_schema_version TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding_dimension INTEGER NOT NULL,
    chunk_policy_version TEXT NOT NULL,
    chunk_size INTEGER NOT NULL,
    chunk_overlap INTEGER NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    state TEXT NOT NULL CHECK (state IN ('BUILDING','VERIFYING','ACTIVE','RETIRED','FAILED')),
    collection_name TEXT NOT NULL,
    alias_name TEXT NOT NULL,
    previous_generation_id TEXT,
    points_count INTEGER DEFAULT 0,
    verification_result JSONB,
    error_message TEXT,

    UNIQUE (alias_name, state) WHERE state = 'ACTIVE'
);

CREATE INDEX IF NOT EXISTS idx_generations_alias_state
ON gptbridge_rag.generations (alias_name, state);
"""

MODEL_REGISTRY_TABLE_SQL = """
-- Model registry for canonical management
CREATE TABLE IF NOT EXISTS gptbridge_rag.model_registry (
    role TEXT NOT NULL,
    model_id TEXT NOT NULL,
    runtime TEXT NOT NULL,
    revision TEXT NOT NULL,
    dimension INTEGER,
    enabled BOOLEAN DEFAULT TRUE,
    canonical BOOLEAN DEFAULT TRUE,
    installed BOOLEAN DEFAULT TRUE,
    verified_at TIMESTAMPTZ,
    PRIMARY KEY (role, model_id)
);

CREATE INDEX IF NOT EXISTS idx_model_registry_role
ON gptbridge_rag.model_registry (role, enabled, canonical);
"""

FULL_SCHEMA_SQL = f"""
-- gptbridge_rag schema
CREATE SCHEMA IF NOT EXISTS gptbridge_rag;

{OUTBOX_TABLE_SQL}

{INDEX_STATE_TABLE_SQL}

{RESOURCE_VERSION_TABLE_SQL}

{CHUNK_TABLE_SQL}

{PROVENANCE_TABLE_SQL}

{EMBEDDING_CACHE_TABLE_SQL}

{GENERATION_TABLE_SQL}

{MODEL_REGISTRY_TABLE_SQL}
"""

# Migration helper
MIGRATION_CHECK_SQL = """
-- Check current schema version
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'gptbridge_rag'
ORDER BY table_name;
"""

__all__ = [
    "OUTBOX_TABLE_SQL",
    "INDEX_STATE_TABLE_SQL",
    "RESOURCE_VERSION_TABLE_SQL",
    "CHUNK_TABLE_SQL",
    "PROVENANCE_TABLE_SQL",
    "EMBEDDING_CACHE_TABLE_SQL",
    "GENERATION_TABLE_SQL",
    "MODEL_REGISTRY_TABLE_SQL",
    "FULL_SCHEMA_SQL",
    "MIGRATION_CHECK_SQL",
]