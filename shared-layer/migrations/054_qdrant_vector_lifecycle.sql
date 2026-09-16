-- 054_qdrant_vector_lifecycle.sql
-- Qdrant Vector Lifecycle.
--
-- Vectors should not exist forever:
--   resource ACTIVE     → vector ACTIVE
--   resource SUPERSEDED → vector STALE
--   resource TOMBSTONED → vector retrieval forbidden
--   retention expired   → delete point
--
-- Order: PG mark → Qdrant delete → verify → PG confirm.
-- Never delete Qdrant first.
--
-- Codex basis:
--   A52/E38 — RAG: Qdrant canonical semantic index.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- qdrant_vector_lifecycle — tracks vector lifecycle tied to resource
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.qdrant_vector_lifecycle (
    resource_id text PRIMARY KEY,
    collection_name text NOT NULL,
    point_id text NOT NULL,
    vector_state text NOT NULL DEFAULT 'ACTIVE' CHECK (vector_state IN (
        'ACTIVE', 'STALE', 'RETRIEVAL_FORBIDDEN', 'DELETE_PENDING',
        'DELETED', 'VERIFIED_DELETED'
    )),
    resource_lifecycle_state text NOT NULL DEFAULT 'ACTIVE',
    pg_marked_at timestamptz,
    qdrant_deleted_at timestamptz,
    verified_at timestamptz,
    pg_confirmed_at timestamptz,
    deletion_reason text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.qdrant_vector_lifecycle ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.qdrant_vector_lifecycle FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS qdrant_vec_lc_read ON gptbridge_index.qdrant_vector_lifecycle;
CREATE POLICY qdrant_vec_lc_read ON gptbridge_index.qdrant_vector_lifecycle
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS qdrant_vec_lc_write ON gptbridge_index.qdrant_vector_lifecycle;
CREATE POLICY qdrant_vec_lc_write ON gptbridge_index.qdrant_vector_lifecycle
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.qdrant_vector_lifecycle FROM PUBLIC;
GRANT SELECT ON gptbridge_index.qdrant_vector_lifecycle TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.qdrant_vector_lifecycle
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS qdrant_vec_lc_state_idx
    ON gptbridge_index.qdrant_vector_lifecycle (vector_state);
CREATE INDEX IF NOT EXISTS qdrant_vec_lc_collection_idx
    ON gptbridge_index.qdrant_vector_lifecycle (collection_name, vector_state);

-- ============================================================================
-- mark_vector_for_resource_state() — sync vector state with resource lifecycle
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.mark_vector_for_resource_state(
    p_resource_id text,
    p_resource_lifecycle_state text
) RETURNS void AS $$
DECLARE
    v_vector_state text;
BEGIN
    CASE p_resource_lifecycle_state
        WHEN 'ACTIVE' THEN v_vector_state := 'ACTIVE';
        WHEN 'STALE' THEN v_vector_state := 'STALE';
        WHEN 'SUPERSEDED' THEN v_vector_state := 'STALE';
        WHEN 'TOMBSTONED' THEN v_vector_state := 'RETRIEVAL_FORBIDDEN';
        WHEN 'ARCHIVED' THEN v_vector_state := 'DELETE_PENDING';
        WHEN 'PURGED' THEN v_vector_state := 'DELETE_PENDING';
        ELSE v_vector_state := 'STALE';
    END CASE;

    UPDATE gptbridge_index.qdrant_vector_lifecycle
    SET vector_state = v_vector_state,
        resource_lifecycle_state = p_resource_lifecycle_state,
        pg_marked_at = CASE WHEN p_resource_lifecycle_state IN ('TOMBSTONED', 'ARCHIVED', 'PURGED')
                            THEN COALESCE(pg_marked_at, now()) ELSE pg_marked_at END,
        updated_at = now()
    WHERE resource_id = p_resource_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- confirm_vector_deleted() — confirm Qdrant deletion is verified
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.confirm_vector_deleted(
    p_resource_id text
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.qdrant_vector_lifecycle
    SET vector_state = 'VERIFIED_DELETED',
        qdrant_deleted_at = COALESCE(qdrant_deleted_at, now()),
        verified_at = now(),
        pg_confirmed_at = now(),
        updated_at = now()
    WHERE resource_id = p_resource_id
      AND vector_state IN ('DELETE_PENDING', 'DELETED');
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_vectors_pending_deletion() — vectors waiting for Qdrant delete
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_vectors_pending_deletion(
    p_limit integer DEFAULT 100
) RETURNS TABLE (
    resource_id text,
    collection_name text,
    point_id text,
    pg_marked_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT resource_id, collection_name, point_id, pg_marked_at
    FROM gptbridge_index.qdrant_vector_lifecycle
    WHERE vector_state = 'DELETE_PENDING'
    ORDER BY pg_marked_at
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
