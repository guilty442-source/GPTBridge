-- 124_data_layer_dependency_graph.sql
-- Data Layer Dependency Graph.
--
-- The converged dependency graph:
--   governance_codex → identity/permission → PostgreSQL central authority →
--   transport/audit/resource_index/RAG_metadata/workflow_state →
--   module SQLite + Qdrant → reconcile + semantic_index →
--   read_models → UI
--
-- Authority boundaries are explicit.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist.
--   A44/E30 — four-functions-local.
--   A52/E38 — RAG: Qdrant canonical semantic index.

CREATE TABLE IF NOT EXISTS gptbridge_index.data_layer_dependency_graph (
    edge_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    from_component text NOT NULL,
    to_component text NOT NULL,
    edge_type text NOT NULL CHECK (edge_type IN (
        'depends_on', 'authority_over', 'feeds_into',
        'reconciles_to', 'derives_from', 'fallback_for'
    )),
    boundary text,  -- 'structured_authority', 'semantic_canonical',
                    -- 'bounded_local_state', 'non_canonical', 'derived'
    description text,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (from_component, to_component, edge_type)
);

ALTER TABLE gptbridge_index.data_layer_dependency_graph ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.data_layer_dependency_graph FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS dep_graph_read ON gptbridge_index.data_layer_dependency_graph;
CREATE POLICY dep_graph_read ON gptbridge_index.data_layer_dependency_graph
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS dep_graph_write ON gptbridge_index.data_layer_dependency_graph;
CREATE POLICY dep_graph_write ON gptbridge_index.data_layer_dependency_graph
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.data_layer_dependency_graph FROM PUBLIC;
GRANT SELECT ON gptbridge_index.data_layer_dependency_graph TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.data_layer_dependency_graph
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS dep_graph_from_idx
    ON gptbridge_index.data_layer_dependency_graph (from_component);
CREATE INDEX IF NOT EXISTS dep_graph_to_idx
    ON gptbridge_index.data_layer_dependency_graph (to_component);

-- Pre-populate the converged dependency graph
INSERT INTO gptbridge_index.data_layer_dependency_graph (
    from_component, to_component, edge_type, boundary, description
) VALUES
    ('governance_codex', 'identity_permission', 'depends_on', 'structured_authority',
     'Codex must be validated before identity/permission'),
    ('identity_permission', 'postgresql', 'depends_on', 'structured_authority',
     'Identity and permission must be ready before PG authority'),
    ('postgresql', 'gptbridge_index', 'authority_over', 'structured_authority',
     'PG is central structured authority for index schema'),
    ('postgresql', 'gptbridge_transport', 'authority_over', 'structured_authority',
     'PG is central structured authority for transport'),
    ('postgresql', 'gptbridge_audit', 'authority_over', 'structured_authority',
     'PG is central structured authority for audit'),
    ('postgresql', 'gptbridge_rag', 'authority_over', 'structured_authority',
     'PG is central structured authority for RAG metadata'),
    ('postgresql', 'gptbridge_identity', 'authority_over', 'structured_authority',
     'PG is central structured authority for identity'),
    ('postgresql', 'workflow_state', 'authority_over', 'structured_authority',
     'PG is central structured authority for workflow state'),
    ('postgresql', 'module_sqlite', 'feeds_into', 'bounded_local_state',
     'Module SQLite feeds into PG via reconcile'),
    ('module_sqlite', 'reconcile', 'reconciles_to', 'bounded_local_state',
     'SQLite reconciles to PG (single direction)'),
    ('postgresql', 'qdrant', 'feeds_into', 'semantic_canonical',
     'PG metadata feeds Qdrant semantic index'),
    ('qdrant', 'semantic_index', 'authority_over', 'semantic_canonical',
     'Qdrant is canonical semantic index (not structured authority)'),
    ('reconcile', 'postgresql', 'reconciles_to', 'structured_authority',
     'Reconcile target is always PG'),
    ('semantic_index', 'read_models', 'feeds_into', 'derived',
     'Semantic index feeds read models'),
    ('postgresql', 'read_models', 'feeds_into', 'derived',
     'PG feeds read models'),
    ('read_models', 'ui', 'feeds_into', 'derived',
     'Read models feed UI'),
    ('local_vector', 'qdrant', 'fallback_for', 'non_canonical',
     'LOCAL-VECTOR is bounded fallback for Qdrant (non-canonical)')
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION gptbridge_index.get_dependencies(
    p_component text
) RETURNS TABLE (
    to_component text, edge_type text, boundary text
) AS $$
BEGIN
    RETURN QUERY
    SELECT to_component, edge_type, boundary
    FROM gptbridge_index.data_layer_dependency_graph
    WHERE from_component = p_component
    ORDER BY edge_type, to_component;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.get_dependents(
    p_component text
) RETURNS TABLE (
    from_component text, edge_type text, boundary text
) AS $$
BEGIN
    RETURN QUERY
    SELECT from_component, edge_type, boundary
    FROM gptbridge_index.data_layer_dependency_graph
    WHERE to_component = p_component
    ORDER BY edge_type, from_component;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
