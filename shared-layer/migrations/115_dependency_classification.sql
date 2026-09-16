-- 115_dependency_classification.sql
-- Dependency Classification.
--
-- Each data component is classified as:
--   authority  — central structured authority (PostgreSQL)
--   required   — required for startup, failure blocks
--   degradable — can degrade to fallback
--   optional   — failure only affects specific capability
--
-- SQLite sub-classes: codex_authority, module_private, runtime_checkpoint,
--                     fallback_cache
-- Qdrant: semantic canonical index (not central structured authority)
-- LOCAL-VECTOR: bounded fallback, non-canonical
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A44/E30 — four-functions-local.
--   A52/E38 — RAG: Qdrant canonical semantic index.

CREATE TABLE IF NOT EXISTS gptbridge_index.dependency_classification (
    classification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    component_name text NOT NULL UNIQUE,
    dependency_type text NOT NULL CHECK (dependency_type IN (
        'authority', 'required', 'degradable', 'optional'
    )),
    component_category text NOT NULL CHECK (component_category IN (
        'postgresql', 'sqlite_codex', 'sqlite_module_private',
        'sqlite_runtime_checkpoint', 'sqlite_fallback_cache',
        'qdrant', 'local_vector', 'transport', 'audit',
        'reconcile', 'rag_metadata', 'read_model', 'cache'
    )),
    failure_effect text NOT NULL CHECK (failure_effect IN (
        'blocks_startup', 'blocks_capability', 'degrades_module',
        'rebuild_required', 'reset_required', 'ignore'
    )),
    criticality text NOT NULL DEFAULT 'capability-critical' CHECK (criticality IN (
        'core-critical', 'capability-critical', 'optional'
    )),
    fallback_component text,
    fallback_boundary text,  -- 'bounded_local_state', 'non_canonical'
    description text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.dependency_classification ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.dependency_classification FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS dep_class_read ON gptbridge_index.dependency_classification;
CREATE POLICY dep_class_read ON gptbridge_index.dependency_classification
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS dep_class_write ON gptbridge_index.dependency_classification;
CREATE POLICY dep_class_write ON gptbridge_index.dependency_classification
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.dependency_classification FROM PUBLIC;
GRANT SELECT ON gptbridge_index.dependency_classification TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.dependency_classification
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS dep_class_type_idx
    ON gptbridge_index.dependency_classification (dependency_type, criticality);
CREATE INDEX IF NOT EXISTS dep_class_category_idx
    ON gptbridge_index.dependency_classification (component_category);

-- Pre-populate core classifications
INSERT INTO gptbridge_index.dependency_classification (
    component_name, dependency_type, component_category,
    failure_effect, criticality, description
) VALUES
    ('postgresql', 'authority', 'postgresql', 'blocks_startup',
     'core-critical', 'Central structured authority'),
    ('governance_codex_sqlite', 'authority', 'sqlite_codex', 'blocks_startup',
     'core-critical', 'Official read-only codex SQLite (special: read-only authority)'),
    ('identity_directory', 'required', 'sqlite_module_private', 'blocks_startup',
     'core-critical', 'Identity and permission directory'),
    ('module_private_sqlite', 'degradable', 'sqlite_module_private', 'degrades_module',
     'capability-critical', 'Module-private formal state'),
    ('runtime_checkpoint_sqlite', 'degradable', 'sqlite_runtime_checkpoint',
     'reset_required', 'optional', 'Runtime/checkpoint SQLite (can reset/recover)'),
    ('fallback_cache_sqlite', 'optional', 'sqlite_fallback_cache', 'rebuild_required',
     'optional', 'Fallback/cache SQLite (can rebuild)'),
    ('qdrant', 'required', 'qdrant', 'blocks_capability',
     'capability-critical', 'Canonical semantic index (not central structured authority)'),
    ('local_vector', 'optional', 'local_vector', 'ignore',
     'optional', 'Bounded fallback, non-canonical'),
    ('transport', 'required', 'transport', 'blocks_capability',
     'core-critical', 'Transport request pipeline'),
    ('audit', 'required', 'audit', 'blocks_startup',
     'core-critical', 'Central audit ledger'),
    ('reconcile', 'degradable', 'reconcile', 'degrades_module',
     'capability-critical', 'SQLite to PostgreSQL reconcile'),
    ('rag_metadata', 'required', 'rag_metadata', 'blocks_capability',
     'capability-critical', 'PostgreSQL RAG metadata authority'),
    ('read_model', 'optional', 'read_model', 'rebuild_required',
     'optional', 'Derived read models (rebuildable)'),
    ('cache', 'optional', 'cache', 'rebuild_required',
     'optional', 'Cache layer (rebuildable)')
ON CONFLICT (component_name) DO NOTHING;

CREATE OR REPLACE FUNCTION gptbridge_index.classify_dependency(
    p_component_name text,
    p_dependency_type text,
    p_component_category text,
    p_failure_effect text,
    p_criticality text DEFAULT 'capability-critical',
    p_fallback_component text DEFAULT NULL,
    p_fallback_boundary text DEFAULT NULL,
    p_description text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.dependency_classification (
        component_name, dependency_type, component_category,
        failure_effect, criticality, fallback_component,
        fallback_boundary, description
    )
    VALUES (
        p_component_name, p_dependency_type, p_component_category,
        p_failure_effect, p_criticality, p_fallback_component,
        p_fallback_boundary, p_description
    )
    ON CONFLICT (component_name) DO UPDATE SET
        dependency_type = EXCLUDED.dependency_type,
        component_category = EXCLUDED.component_category,
        failure_effect = EXCLUDED.failure_effect,
        criticality = EXCLUDED.criticality,
        fallback_component = EXCLUDED.fallback_component,
        fallback_boundary = EXCLUDED.fallback_boundary,
        description = EXCLUDED.description,
        updated_at = now()
    RETURNING classification_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.get_dependency_classification(
    p_component_name text
) RETURNS TABLE (
    dependency_type text, component_category text,
    failure_effect text, criticality text
) AS $$
BEGIN
    RETURN QUERY
    SELECT dependency_type, component_category, failure_effect, criticality
    FROM gptbridge_index.dependency_classification
    WHERE component_name = p_component_name;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
