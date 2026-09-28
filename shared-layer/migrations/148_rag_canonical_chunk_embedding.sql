-- 148_rag_canonical_chunk_embedding.sql
-- B61/C56/C62/C63: PostgreSQL is the sole canonical durable vector
-- authority; the Rust vector engine holds a rebuildable derived
-- projection only.  Persist the canonical embedding bytes on the
-- canonical chunk row so the derived projection is rebuilt by COPY of
-- canonical values, not by re-derivation.  Format: float64
-- little-endian packed array, matching the BYTEA convention used by
-- shared_layer.local.vector_store.  A pgvector-native ``vector`` column
-- supersedes this column when the extension is admitted to the host
-- (extension binaries are absent on the current PostgreSQL 18.6
-- installation); this column is the migration-safe canonical substrate.

ALTER TABLE gptbridge_rag.chunk
    ADD COLUMN IF NOT EXISTS embedding BYTEA;
