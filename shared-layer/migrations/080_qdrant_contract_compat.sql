-- 080_qdrant_contract_compat.sql
-- Qdrant Contract Compatibility.
--
-- Before upgrading Qdrant, check:
--   collection schema, payload filter, snapshot format,
--   index config, client API, point ID behavior, recovery
--
-- Codex basis:
--   A52/E38 — RAG: Qdrant canonical semantic index.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- qdrant_contract_compat — Qdrant version compatibility checks
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.qdrant_contract_compat (
    compat_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    from_version text NOT NULL,
    to_version text NOT NULL,
    check_category text NOT NULL CHECK (check_category IN (
        'collection_schema', 'payload_filter', 'snapshot_format',
        'index_config', 'client_api', 'point_id_behavior',
        'recovery', 'distance_metric', 'vector_dimension'
    )),
    passed boolean NOT NULL DEFAULT false,
    tested_at timestamptz NOT NULL DEFAULT now(),
    tested_by text NOT NULL,
    test_details jsonb,
    failure_reason text,
    migration_notes text
);

ALTER TABLE gptbridge_index.qdrant_contract_compat ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.qdrant_contract_compat FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS qdrant_compat_read ON gptbridge_index.qdrant_contract_compat;
CREATE POLICY qdrant_compat_read ON gptbridge_index.qdrant_contract_compat
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS qdrant_compat_write ON gptbridge_index.qdrant_contract_compat;
CREATE POLICY qdrant_compat_write ON gptbridge_index.qdrant_contract_compat
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.qdrant_contract_compat FROM PUBLIC;
GRANT SELECT ON gptbridge_index.qdrant_contract_compat TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.qdrant_contract_compat
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS qdrant_compat_version_idx
    ON gptbridge_index.qdrant_contract_compat (from_version, to_version, check_category);
CREATE INDEX IF NOT EXISTS qdrant_compat_passed_idx
    ON gptbridge_index.qdrant_contract_compat (passed, tested_at);

-- ============================================================================
-- record_qdrant_compat() — record a Qdrant compatibility check
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_qdrant_compat(
    p_from_version text,
    p_to_version text,
    p_check_category text,
    p_passed boolean,
    p_tested_by text,
    p_test_details jsonb DEFAULT NULL,
    p_failure_reason text DEFAULT NULL,
    p_migration_notes text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.qdrant_contract_compat (
        from_version, to_version, check_category, passed,
        tested_by, test_details, failure_reason, migration_notes
    )
    VALUES (
        p_from_version, p_to_version, p_check_category, p_passed,
        p_tested_by, p_test_details, p_failure_reason, p_migration_notes
    )
    RETURNING compat_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- is_qdrant_upgrade_safe() — check if all checks passed for an upgrade
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.is_qdrant_upgrade_safe(
    p_from_version text,
    p_to_version text
) RETURNS boolean AS $$
DECLARE
    v_total integer;
    v_passed integer;
BEGIN
    SELECT count(*) INTO v_total
    FROM gptbridge_index.qdrant_contract_compat
    WHERE from_version = p_from_version AND to_version = p_to_version;

    SELECT count(*) INTO v_passed
    FROM gptbridge_index.qdrant_contract_compat
    WHERE from_version = p_from_version AND to_version = p_to_version
      AND passed = true;

    RETURN v_total > 0 AND v_total = v_passed;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
