-- 009_rag_canonical_execution_columns.sql
-- A371-A374: extend gptbridge_rag.index_state with the authoritative
-- per-index provenance columns required by the canonical RAG path, and add
-- a generated FTS column on gptbridge_rag.chunk so PostgreSQL is the live
-- metadata/FTS/index_state authority (A374 step 2).
-- Chunk text stays inside chunk.metadata->>'content', mirroring the local
-- degraded store convention; the tsvector column is generated from it.

ALTER TABLE gptbridge_rag.index_state
    ADD COLUMN IF NOT EXISTS embedding_dimension integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS chunk_size integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS chunk_overlap integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS content_hash text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS qdrant_point_id text NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS postgresql_record_id text,
    ADD COLUMN IF NOT EXISTS source_revision bigint NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS tombstone_generation integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS embedding_version integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS chunking_version integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS parser_version integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS rag_schema_version integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS pipeline_version integer NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS backend_generation integer NOT NULL DEFAULT 1;

ALTER TABLE gptbridge_rag.chunk
    ADD COLUMN IF NOT EXISTS content_tsv tsvector
        GENERATED ALWAYS AS (
            to_tsvector('simple', coalesce(metadata ->> 'content', ''))
        ) STORED;

CREATE INDEX IF NOT EXISTS rag_chunk_fts_idx
    ON gptbridge_rag.chunk USING gin (content_tsv);

CREATE INDEX IF NOT EXISTS rag_chunk_module_fts_idx
    ON gptbridge_rag.chunk (module_id)
    WHERE content_tsv IS NOT NULL;
