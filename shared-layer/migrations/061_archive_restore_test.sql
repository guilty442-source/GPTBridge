-- 061_archive_restore_test.sql
-- Archive Restore Test.
--
-- Archives cannot just verify hash.  Periodically:
--   sample archive → restore to temporary database → schema check →
--   row count → hash verify → query test
--
-- Only then is it a valid archive.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- archive_restore_test — records restore test results
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.archive_restore_test (
    test_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    archive_id uuid NOT NULL REFERENCES gptbridge_index.archive_catalog(archive_id),
    tested_at timestamptz NOT NULL DEFAULT now(),
    tested_by text NOT NULL,
    temporary_database text NOT NULL,
    schema_check_passed boolean NOT NULL DEFAULT false,
    row_count_match boolean NOT NULL DEFAULT false,
    hash_verify_passed boolean NOT NULL DEFAULT false,
    query_test_passed boolean NOT NULL DEFAULT false,
    expected_record_count integer,
    actual_record_count integer,
    expected_hash text,
    actual_hash text,
    test_result jsonb,
    overall_passed boolean NOT NULL DEFAULT false,
    failure_reason text
);

ALTER TABLE gptbridge_index.archive_restore_test ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.archive_restore_test FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS archive_restore_read ON gptbridge_index.archive_restore_test;
CREATE POLICY archive_restore_read ON gptbridge_index.archive_restore_test
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS archive_restore_write ON gptbridge_index.archive_restore_test;
CREATE POLICY archive_restore_write ON gptbridge_index.archive_restore_test
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.archive_restore_test FROM PUBLIC;
GRANT SELECT ON gptbridge_index.archive_restore_test TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.archive_restore_test
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS archive_restore_test_idx
    ON gptbridge_index.archive_restore_test (archive_id, tested_at);
CREATE INDEX IF NOT EXISTS archive_restore_passed_idx
    ON gptbridge_index.archive_restore_test (overall_passed, tested_at);

-- ============================================================================
-- record_restore_test() — record a restore test result
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_restore_test(
    p_archive_id uuid,
    p_tested_by text,
    p_temporary_database text,
    p_schema_check_passed boolean,
    p_row_count_match boolean,
    p_hash_verify_passed boolean,
    p_query_test_passed boolean,
    p_expected_record_count integer DEFAULT NULL,
    p_actual_record_count integer DEFAULT NULL,
    p_expected_hash text DEFAULT NULL,
    p_actual_hash text DEFAULT NULL,
    p_test_result jsonb DEFAULT NULL,
    p_failure_reason text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_overall boolean;
BEGIN
    v_overall := p_schema_check_passed AND p_row_count_match
                 AND p_hash_verify_passed AND p_query_test_passed;

    INSERT INTO gptbridge_index.archive_restore_test (
        archive_id, tested_by, temporary_database,
        schema_check_passed, row_count_match, hash_verify_passed,
        query_test_passed, expected_record_count, actual_record_count,
        expected_hash, actual_hash, test_result,
        overall_passed, failure_reason
    )
    VALUES (
        p_archive_id, p_tested_by, p_temporary_database,
        p_schema_check_passed, p_row_count_match, p_hash_verify_passed,
        p_query_test_passed, p_expected_record_count, p_actual_record_count,
        p_expected_hash, p_actual_hash, p_test_result,
        v_overall, p_failure_reason
    )
    RETURNING test_id INTO v_id;

    -- Update archive_catalog with restore test info
    PERFORM gptbridge_index.mark_restore_tested(
        p_archive_id,
        jsonb_build_object('test_id', v_id, 'passed', v_overall)
    );

    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_failed_restore_tests() — archives that failed restore testing
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_failed_restore_tests(
    p_limit integer DEFAULT 50
) RETURNS TABLE (
    test_id uuid,
    archive_id uuid,
    tested_at timestamptz,
    failure_reason text
) AS $$
BEGIN
    RETURN QUERY
    SELECT test_id, archive_id, tested_at, failure_reason
    FROM gptbridge_index.archive_restore_test
    WHERE overall_passed = false
    ORDER BY tested_at DESC
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
