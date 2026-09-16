-- 046_qdrant_contract_version.sql
-- Qdrant Contract Versioning.
--
-- Qdrant is not just a collection name.  Contract includes:
--   collection_name, vector_dimension, distance_metric, embedding_model,
--   payload_schema, required module_id, resource_id format, chunk_id
--   format, revision field.
--
-- When embedding model changes:
--   collection_v1 → build collection_v2 → re-embed → verify →
--   switch index_state → retire v1
--
-- Codex basis:
--   A52/E38 — RAG: Qdrant canonical semantic index.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- qdrant_contract — versioned Qdrant collection contracts
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.qdrant_contract (
    contract_version integer PRIMARY KEY,
    collection_name text NOT NULL,
    vector_dimension integer NOT NULL,
    distance_metric text NOT NULL CHECK (distance_metric IN (
        'Cosine', 'Dot', 'Euclid'
    )),
    embedding_model text NOT NULL,
    payload_schema jsonb NOT NULL DEFAULT '{}'::jsonb,
    required_module_id text,
    resource_id_format text NOT NULL DEFAULT 'uuid',
    chunk_id_format text NOT NULL DEFAULT 'uuid',
    revision_field text NOT NULL DEFAULT 'revision',
    introduced_in_release text REFERENCES gptbridge_index.database_release(release_id),
    status text NOT NULL DEFAULT 'active' CHECK (status IN (
        'active', 'deprecated', 'retired'
    )),
    deprecated_at timestamptz,
    retired_at timestamptz,
    successor_version integer,
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.qdrant_contract ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.qdrant_contract FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS qdrant_contract_read ON gptbridge_index.qdrant_contract;
CREATE POLICY qdrant_contract_read ON gptbridge_index.qdrant_contract
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS qdrant_contract_write ON gptbridge_index.qdrant_contract;
CREATE POLICY qdrant_contract_write ON gptbridge_index.qdrant_contract
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.qdrant_contract FROM PUBLIC;
GRANT SELECT ON gptbridge_index.qdrant_contract TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.qdrant_contract
    TO gptbridge_index_executor;

-- ============================================================================
-- register_qdrant_contract() — register a new Qdrant contract version
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.register_qdrant_contract(
    p_contract_version integer,
    p_collection_name text,
    p_vector_dimension integer,
    p_distance_metric text,
    p_embedding_model text,
    p_payload_schema jsonb DEFAULT '{}'::jsonb,
    p_required_module_id text DEFAULT NULL,
    p_description text DEFAULT NULL,
    p_introduced_in_release text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    INSERT INTO gptbridge_index.qdrant_contract (
        contract_version, collection_name, vector_dimension,
        distance_metric, embedding_model, payload_schema,
        required_module_id, description, introduced_in_release
    )
    VALUES (
        p_contract_version, p_collection_name, p_vector_dimension,
        p_distance_metric, p_embedding_model, p_payload_schema,
        p_required_module_id, p_description, p_introduced_in_release
    )
    ON CONFLICT (contract_version) DO UPDATE SET
        collection_name = EXCLUDED.collection_name,
        vector_dimension = EXCLUDED.vector_dimension,
        distance_metric = EXCLUDED.distance_metric,
        embedding_model = EXCLUDED.embedding_model,
        payload_schema = EXCLUDED.payload_schema,
        required_module_id = EXCLUDED.required_module_id,
        description = EXCLUDED.description,
        updated_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- deprecate_qdrant_contract() — deprecate a Qdrant contract version
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.deprecate_qdrant_contract(
    p_contract_version integer,
    p_successor_version integer
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.qdrant_contract
    SET status = 'deprecated',
        deprecated_at = now(),
        successor_version = p_successor_version,
        updated_at = now()
    WHERE contract_version = p_contract_version;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- retire_qdrant_contract() — retire a Qdrant contract version
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.retire_qdrant_contract(
    p_contract_version integer
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.qdrant_contract
    SET status = 'retired',
        retired_at = now(),
        updated_at = now()
    WHERE contract_version = p_contract_version;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_active_qdrant_contract() — get the active Qdrant contract
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_active_qdrant_contract()
RETURNS TABLE (
    contract_version integer,
    collection_name text,
    vector_dimension integer,
    distance_metric text,
    embedding_model text
) AS $$
BEGIN
    RETURN QUERY
    SELECT contract_version, collection_name, vector_dimension,
           distance_metric, embedding_model
    FROM gptbridge_index.qdrant_contract
    WHERE status = 'active'
    ORDER BY contract_version DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
