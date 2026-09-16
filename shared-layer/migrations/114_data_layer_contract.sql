-- 114_data_layer_contract.sql
-- Data Layer Master Contract.
--
-- Converges all PostgreSQL, SQLite, Qdrant, Reconcile, RAG, Transport,
-- Audit, Cache, Recovery rules into one executable runtime contract.
--
-- This contract does NOT replace the codex. It is the runtime data layer
-- execution contract.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist.
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.data_layer_contract (
    contract_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    contract_version integer NOT NULL,
    database_release_id text,  -- links to database_release.release_id
    postgresql_schema_version integer,
    sqlite_template_version integer,
    qdrant_contract_version integer,
    security_generation integer,
    data_generation integer,
    required_capabilities text[] NOT NULL DEFAULT '{}',
    optional_capabilities text[] NOT NULL DEFAULT '{}',
    startup_order text[] NOT NULL DEFAULT '{}',
    shutdown_order text[] NOT NULL DEFAULT '{}',
    degradation_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    recovery_policy jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft', 'validated', 'active', 'superseded', 'retired'
    )),
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.data_layer_contract ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.data_layer_contract FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS data_layer_contract_read ON gptbridge_index.data_layer_contract;
CREATE POLICY data_layer_contract_read ON gptbridge_index.data_layer_contract
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS data_layer_contract_write ON gptbridge_index.data_layer_contract;
CREATE POLICY data_layer_contract_write ON gptbridge_index.data_layer_contract
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.data_layer_contract FROM PUBLIC;
GRANT SELECT ON gptbridge_index.data_layer_contract TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.data_layer_contract
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS data_layer_contract_status_idx
    ON gptbridge_index.data_layer_contract (status, updated_at);
CREATE INDEX IF NOT EXISTS data_layer_contract_version_idx
    ON gptbridge_index.data_layer_contract (contract_version);

CREATE OR REPLACE FUNCTION gptbridge_index.register_data_layer_contract(
    p_contract_version integer,
    p_database_release_id text DEFAULT NULL,
    p_postgresql_schema_version integer DEFAULT NULL,
    p_sqlite_template_version integer DEFAULT NULL,
    p_qdrant_contract_version integer DEFAULT NULL,
    p_security_generation integer DEFAULT NULL,
    p_data_generation integer DEFAULT NULL,
    p_required_capabilities text[] DEFAULT '{}',
    p_optional_capabilities text[] DEFAULT '{}',
    p_startup_order text[] DEFAULT '{}',
    p_shutdown_order text[] DEFAULT '{}',
    p_degradation_policy jsonb DEFAULT '{}'::jsonb,
    p_recovery_policy jsonb DEFAULT '{}'::jsonb
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.data_layer_contract (
        contract_version, database_release_id,
        postgresql_schema_version, sqlite_template_version,
        qdrant_contract_version, security_generation, data_generation,
        required_capabilities, optional_capabilities,
        startup_order, shutdown_order,
        degradation_policy, recovery_policy
    )
    VALUES (
        p_contract_version, p_database_release_id,
        p_postgresql_schema_version, p_sqlite_template_version,
        p_qdrant_contract_version, p_security_generation, p_data_generation,
        p_required_capabilities, p_optional_capabilities,
        p_startup_order, p_shutdown_order,
        p_degradation_policy, p_recovery_policy
    )
    RETURNING contract_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.activate_data_layer_contract(
    p_contract_id uuid
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.data_layer_contract
    SET status = 'superseded', updated_at = now()
    WHERE status = 'active' AND contract_id != p_contract_id;

    UPDATE gptbridge_index.data_layer_contract
    SET status = 'active', activated_at = now(), updated_at = now()
    WHERE contract_id = p_contract_id AND status = 'validated';
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.get_active_data_layer_contract()
RETURNS TABLE (
    contract_id uuid, contract_version integer,
    database_release_id text, postgresql_schema_version integer,
    sqlite_template_version integer, qdrant_contract_version integer,
    security_generation integer, data_generation integer,
    required_capabilities text[], optional_capabilities text[],
    startup_order text[], shutdown_order text[]
) AS $$
BEGIN
    RETURN QUERY
    SELECT contract_id, contract_version, database_release_id,
           postgresql_schema_version, sqlite_template_version,
           qdrant_contract_version, security_generation, data_generation,
           required_capabilities, optional_capabilities,
           startup_order, shutdown_order
    FROM gptbridge_index.data_layer_contract
    WHERE status = 'active'
    ORDER BY contract_version DESC LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
