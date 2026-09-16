-- 068_qdrant_integrity_mapping.sql
-- Qdrant Integrity Mapping.
--
-- PostgreSQL chunk stores:
--   chunk_hash, embedding_version, qdrant_point_id, resource_revision
--
-- Can check: PG chunk -> Qdrant point still maps to same version.
--
-- Codex basis:
--   A52/E38 — RAG: Qdrant canonical semantic index.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- Add integrity columns to gptbridge_rag.chunk
-- ============================================================================
ALTER TABLE gptbridge_rag.chunk
    ADD COLUMN IF NOT EXISTS chunk_hash text;
ALTER TABLE gptbridge_rag.chunk
    ADD COLUMN IF NOT EXISTS embedding_version integer NOT NULL DEFAULT 1;
ALTER TABLE gptbridge_rag.chunk
    ADD COLUMN IF NOT EXISTS qdrant_point_id text;
ALTER TABLE gptbridge_rag.chunk
    ADD COLUMN IF NOT EXISTS resource_revision integer;

CREATE INDEX IF NOT EXISTS chunk_hash_idx
    ON gptbridge_rag.chunk (chunk_hash);
CREATE INDEX IF NOT EXISTS chunk_qdrant_point_idx
    ON gptbridge_rag.chunk (qdrant_point_id);
CREATE INDEX IF NOT EXISTS chunk_embedding_version_idx
    ON gptbridge_rag.chunk (embedding_version);

-- ============================================================================
-- qdrant_integrity_map — tracks PG chunk <-> Qdrant point integrity
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.qdrant_integrity_map (
    chunk_id uuid PRIMARY KEY,
    resource_id text NOT NULL,
    chunk_hash text NOT NULL,
    embedding_version integer NOT NULL DEFAULT 1,
    qdrant_point_id text NOT NULL,
    resource_revision integer NOT NULL,
    pg_recorded_at timestamptz NOT NULL DEFAULT now(),
    qdrant_verified_at timestamptz,
    qdrant_point_exists boolean,
    hash_match boolean,
    version_match boolean,
    integrity_state text NOT NULL DEFAULT 'unverified' CHECK (integrity_state IN (
        'verified', 'unverified', 'mismatch', 'stale', 'orphaned'
    )),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.qdrant_integrity_map ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.qdrant_integrity_map FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS qdrant_integrity_read ON gptbridge_index.qdrant_integrity_map;
CREATE POLICY qdrant_integrity_read ON gptbridge_index.qdrant_integrity_map
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS qdrant_integrity_write ON gptbridge_index.qdrant_integrity_map;
CREATE POLICY qdrant_integrity_write ON gptbridge_index.qdrant_integrity_map
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.qdrant_integrity_map FROM PUBLIC;
GRANT SELECT ON gptbridge_index.qdrant_integrity_map TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.qdrant_integrity_map
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS qdrant_integrity_state_idx
    ON gptbridge_index.qdrant_integrity_map (integrity_state);
CREATE INDEX IF NOT EXISTS qdrant_integrity_resource_idx
    ON gptbridge_index.qdrant_integrity_map (resource_id);

-- ============================================================================
-- record_qdrant_integrity() — record PG chunk -> Qdrant point mapping
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_qdrant_integrity(
    p_chunk_id uuid,
    p_resource_id text,
    p_chunk_hash text,
    p_embedding_version integer,
    p_qdrant_point_id text,
    p_resource_revision integer
) RETURNS void AS $$
BEGIN
    INSERT INTO gptbridge_index.qdrant_integrity_map (
        chunk_id, resource_id, chunk_hash, embedding_version,
        qdrant_point_id, resource_revision
    )
    VALUES (
        p_chunk_id, p_resource_id, p_chunk_hash, p_embedding_version,
        p_qdrant_point_id, p_resource_revision
    )
    ON CONFLICT (chunk_id) DO UPDATE SET
        chunk_hash = EXCLUDED.chunk_hash,
        embedding_version = EXCLUDED.embedding_version,
        qdrant_point_id = EXCLUDED.qdrant_point_id,
        resource_revision = EXCLUDED.resource_revision,
        updated_at = now(),
        integrity_state = 'unverified';
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- verify_qdrant_integrity() — verify a single chunk's Qdrant mapping
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.verify_qdrant_integrity(
    p_chunk_id uuid,
    p_point_exists boolean,
    p_hash_match boolean,
    p_version_match boolean
) RETURNS void AS $$
DECLARE
    v_state text;
BEGIN
    IF NOT p_point_exists THEN
        v_state := 'orphaned';
    ELSIF NOT p_hash_match THEN
        v_state := 'mismatch';
    ELSIF NOT p_version_match THEN
        v_state := 'stale';
    ELSE
        v_state := 'verified';
    END IF;

    UPDATE gptbridge_index.qdrant_integrity_map
    SET qdrant_verified_at = now(),
        qdrant_point_exists = p_point_exists,
        hash_match = p_hash_match,
        version_match = p_version_match,
        integrity_state = v_state,
        updated_at = now()
    WHERE chunk_id = p_chunk_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_qdrant_integrity_issues() — find chunks with integrity problems
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_qdrant_integrity_issues(
    p_limit integer DEFAULT 100
) RETURNS TABLE (
    chunk_id uuid,
    resource_id text,
    integrity_state text,
    qdrant_point_id text
) AS $$
BEGIN
    RETURN QUERY
    SELECT chunk_id, resource_id, integrity_state, qdrant_point_id
    FROM gptbridge_index.qdrant_integrity_map
    WHERE integrity_state IN ('mismatch', 'stale', 'orphaned')
    ORDER BY updated_at DESC
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
