-- 060_dependency_check.sql
-- Resource Dependency Check.
--
-- Before purge, check:
--   resource_relation, rag.chunk, qdrant point, transport reference,
--   audit reference, locator, module private state
--
-- If dependencies exist, cannot directly purge.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A52/E38 — RAG: Qdrant canonical semantic index.

-- ============================================================================
-- dependency_check — records dependency check results before purge
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.dependency_check (
    check_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id text NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT now(),
    checked_by text NOT NULL,
    has_dependencies boolean NOT NULL DEFAULT false,
    dependency_details jsonb NOT NULL DEFAULT '{}'::jsonb,
    can_purge boolean NOT NULL DEFAULT false,
    purge_queue_id uuid REFERENCES gptbridge_index.purge_queue(queue_id)
);

ALTER TABLE gptbridge_index.dependency_check ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.dependency_check FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS dependency_check_read ON gptbridge_index.dependency_check;
CREATE POLICY dependency_check_read ON gptbridge_index.dependency_check
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS dependency_check_write ON gptbridge_index.dependency_check;
CREATE POLICY dependency_check_write ON gptbridge_index.dependency_check
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.dependency_check FROM PUBLIC;
GRANT SELECT ON gptbridge_index.dependency_check TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.dependency_check
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS dependency_check_resource_idx
    ON gptbridge_index.dependency_check (resource_id, checked_at);

-- ============================================================================
-- check_resource_dependencies() — check all dependencies for a resource
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.check_resource_dependencies(
    p_resource_id text,
    p_checked_by text
) RETURNS TABLE (
    can_purge boolean,
    dependency_count integer,
    details jsonb
) AS $$
DECLARE
    v_details jsonb := '{}'::jsonb;
    v_count integer := 0;
    v_relations integer;
    v_chunks integer;
    v_vectors integer;
    v_transport integer;
    v_audit integer;
    v_locator integer;
BEGIN
    -- Check resource_relation
    SELECT count(*) INTO v_relations
    FROM gptbridge_index.resource_relation
    WHERE source_resource_id = p_resource_id OR target_resource_id = p_resource_id;

    -- Check rag.chunk
    SELECT count(*) INTO v_chunks
    FROM gptbridge_rag.chunk
    WHERE resource_id = p_resource_id;

    -- Check qdrant vector lifecycle
    SELECT count(*) INTO v_vectors
    FROM gptbridge_index.qdrant_vector_lifecycle
    WHERE resource_id = p_resource_id
      AND vector_state NOT IN ('VERIFIED_DELETED');

    -- Check transport references
    SELECT count(*) INTO v_transport
    FROM gptbridge_transport.tool_request
    WHERE target_tool_id = p_resource_id
      AND status IN ('queued', 'claimed');

    -- Check audit references (recent only)
    SELECT count(*) INTO v_audit
    FROM gptbridge_audit.event
    WHERE actor = p_resource_id
      AND occurred_at > now() - '30 days'::interval;

    -- Check locator
    SELECT count(*) INTO v_locator
    FROM gptbridge_index.registry_locations
    WHERE resource_id = p_resource_id;

    v_count := v_relations + v_chunks + v_vectors + v_transport + v_audit + v_locator;
    v_details := jsonb_build_object(
        'resource_relations', v_relations,
        'rag_chunks', v_chunks,
        'qdrant_vectors', v_vectors,
        'transport_refs', v_transport,
        'audit_refs', v_audit,
        'locators', v_locator
    );

    -- Record the check
    INSERT INTO gptbridge_index.dependency_check (
        resource_id, checked_by, has_dependencies,
        dependency_details, can_purge
    )
    VALUES (
        p_resource_id, p_checked_by, v_count > 0,
        v_details, v_count = 0
    );

    RETURN QUERY SELECT v_count = 0, v_count, v_details;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
