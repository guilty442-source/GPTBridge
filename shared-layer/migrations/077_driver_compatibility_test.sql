-- 077_driver_compatibility_test.sql
-- Driver Compatibility Test.
--
-- Before upgrading psycopg, test:
--   connection pool, transaction, row factory, async/sync,
--   notify/listen, error mapping, reconnect, SKIP LOCKED
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist.

-- ============================================================================
-- driver_compatibility_test — records driver compatibility test results
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.driver_compatibility_test (
    test_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    driver_name text NOT NULL,  -- 'psycopg', 'sqlite3', 'qdrant_client'
    driver_version text NOT NULL,
    test_category text NOT NULL CHECK (test_category IN (
        'connection_pool', 'transaction', 'row_factory',
        'async_sync', 'notify_listen', 'error_mapping',
        'reconnect', 'skip_locked', 'prepared_statements'
    )),
    passed boolean NOT NULL DEFAULT false,
    tested_at timestamptz NOT NULL DEFAULT now(),
    tested_by text NOT NULL,
    test_details jsonb,
    failure_reason text,
    duration_ms integer
);

ALTER TABLE gptbridge_index.driver_compatibility_test ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.driver_compatibility_test FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS driver_compat_read ON gptbridge_index.driver_compatibility_test;
CREATE POLICY driver_compat_read ON gptbridge_index.driver_compatibility_test
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS driver_compat_write ON gptbridge_index.driver_compatibility_test;
CREATE POLICY driver_compat_write ON gptbridge_index.driver_compatibility_test
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.driver_compatibility_test FROM PUBLIC;
GRANT SELECT ON gptbridge_index.driver_compatibility_test TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.driver_compatibility_test
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS driver_compat_driver_idx
    ON gptbridge_index.driver_compatibility_test (driver_name, driver_version, tested_at);
CREATE INDEX IF NOT EXISTS driver_compat_passed_idx
    ON gptbridge_index.driver_compatibility_test (driver_name, passed, tested_at);

-- ============================================================================
-- record_driver_test() — record a driver compatibility test result
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_driver_test(
    p_driver_name text,
    p_driver_version text,
    p_test_category text,
    p_passed boolean,
    p_tested_by text,
    p_test_details jsonb DEFAULT NULL,
    p_failure_reason text DEFAULT NULL,
    p_duration_ms integer DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.driver_compatibility_test (
        driver_name, driver_version, test_category, passed,
        tested_by, test_details, failure_reason, duration_ms
    )
    VALUES (
        p_driver_name, p_driver_version, p_test_category, p_passed,
        p_tested_by, p_test_details, p_failure_reason, p_duration_ms
    )
    RETURNING test_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- is_driver_version_verified() — check if all required tests passed for a driver
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.is_driver_version_verified(
    p_driver_name text,
    p_driver_version text
) RETURNS boolean AS $$
DECLARE
    v_total integer;
    v_passed integer;
BEGIN
    SELECT count(*) INTO v_total
    FROM gptbridge_index.driver_compatibility_test
    WHERE driver_name = p_driver_name AND driver_version = p_driver_version;

    SELECT count(*) INTO v_passed
    FROM gptbridge_index.driver_compatibility_test
    WHERE driver_name = p_driver_name AND driver_version = p_driver_version
      AND passed = true;

    RETURN v_total > 0 AND v_total = v_passed;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_failed_driver_tests() — find failed driver tests
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_failed_driver_tests(
    p_driver_name text DEFAULT NULL,
    p_limit integer DEFAULT 100
) RETURNS TABLE (
    test_id uuid,
    driver_name text,
    driver_version text,
    test_category text,
    failure_reason text,
    tested_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT test_id, driver_name, driver_version, test_category,
           failure_reason, tested_at
    FROM gptbridge_index.driver_compatibility_test
    WHERE passed = false
      AND (p_driver_name IS NULL OR driver_name = p_driver_name)
    ORDER BY tested_at DESC
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
