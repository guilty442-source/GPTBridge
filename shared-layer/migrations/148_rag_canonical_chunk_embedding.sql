-- 148_rag_canonical_chunk_embedding.sql
-- B61/C56/C62/C63: PostgreSQL+pgvector is the sole canonical durable
-- vector authority; the Rust vector engine holds a rebuildable derived
-- projection only.  Persist the canonical embedding on the canonical
-- chunk row so the derived projection is rebuilt by COPY of canonical
-- values, not by re-derivation.  The column is dimension-unspecified
-- ``vector`` so a single schema carries every registered embedding
-- dimension; per-resource dimension truth stays in index_state.

CREATE EXTENSION IF NOT EXISTS vector;

ALTER TABLE gptbridge_rag.chunk
    ADD COLUMN IF NOT EXISTS embedding vector;
