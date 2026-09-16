-- 043_query_contract_version.sql
-- Query Contract Versioning.
--
-- Repository should not depend on "these columns happen to exist right now".
-- Query contracts are versioned: resource_lookup_v1, resource_lookup_v2,
-- claim_request_v3, append_audit_v2, etc.
--
-- During migration transition, both old and new versions can coexist
-- until the old version is officially retired.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist; deny-by-default.

-- ============================================================================
-- query_contract — versioned query contracts
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.query_contract (
    contract_name text NOT NULL,  -- e.g. resource_lookup
    version integer NOT NULL,     -- e.g. 1, 2, 3
    full_name text GENERATED ALWAYS AS (contract_name || '_v' || version) STORED,
    sql_template text NOT NULL,
    status text NOT NULL DEFAULT 'active' CHECK (status IN (
        'active', 'deprecated', 'retired'
    )),
    deprecated_at timestamptz,
    retired_at timestamptz,
    successor_version integer,  -- next version after deprecation
    introduced_in_release text REFERENCES gptbridge_index.database_release(release_id),
    retired_in_release text REFERENCES gptbridge_index.database_release(release_id),
    description text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (contract_name, version)
);

ALTER TABLE gptbridge_index.query_contract ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.query_contract FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS query_contract_read ON gptbridge_index.query_contract;
CREATE POLICY query_contract_read ON gptbridge_index.query_contract
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS query_contract_write ON gptbridge_index.query_contract;
CREATE POLICY query_contract_write ON gptbridge_index.query_contract
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.query_contract FROM PUBLIC;
GRANT SELECT ON gptbridge_index.query_contract TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.query_contract
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS query_contract_name_idx
    ON gptbridge_index.query_contract (contract_name, version);
CREATE INDEX IF NOT EXISTS query_contract_status_idx
    ON gptbridge_index.query_contract (status);

-- ============================================================================
-- register_query_contract() — register a new query contract version
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.register_query_contract(
    p_contract_name text,
    p_version integer,
    p_sql_template text,
    p_description text DEFAULT NULL,
    p_introduced_in_release text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    INSERT INTO gptbridge_index.query_contract (
        contract_name, version, sql_template, description, introduced_in_release
    )
    VALUES (
        p_contract_name, p_version, p_sql_template,
        p_description, p_introduced_in_release
    )
    ON CONFLICT (contract_name, version) DO UPDATE SET
        sql_template = EXCLUDED.sql_template,
        description = EXCLUDED.description,
        updated_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- deprecate_query_contract() — mark a version as deprecated
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.deprecate_query_contract(
    p_contract_name text,
    p_version integer,
    p_successor_version integer
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.query_contract
    SET status = 'deprecated',
        deprecated_at = now(),
        successor_version = p_successor_version,
        updated_at = now()
    WHERE contract_name = p_contract_name AND version = p_version;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- retire_query_contract() — mark a version as retired
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.retire_query_contract(
    p_contract_name text,
    p_version integer,
    p_retired_in_release text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.query_contract
    SET status = 'retired',
        retired_at = now(),
        retired_in_release = p_retired_in_release,
        updated_at = now()
    WHERE contract_name = p_contract_name AND version = p_version;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_active_query_contract() — get the active version of a contract
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_active_query_contract(
    p_contract_name text
) RETURNS TABLE (
    version integer,
    sql_template text,
    description text
) AS $$
BEGIN
    RETURN QUERY
    SELECT version, sql_template, description
    FROM gptbridge_index.query_contract
    WHERE contract_name = p_contract_name AND status = 'active'
    ORDER BY version DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
