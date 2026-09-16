-- 075_compatibility_matrix_ext.sql
-- Extended Compatibility Matrix.
--
-- Which combinations are tested, which are forbidden:
--   PostgreSQL 18.x + psycopg 3.x + Qdrant x.y + SQLite z
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist.

-- ============================================================================
-- compatibility_matrix_ext — tested/forbidden engine+driver combinations
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.compatibility_matrix_ext (
    matrix_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id text REFERENCES gptbridge_index.database_release(release_id),
    postgresql_version text NOT NULL,
    psycopg_version text NOT NULL,
    sqlite_runtime_version text NOT NULL,
    qdrant_server_version text NOT NULL,
    qdrant_client_version text,
    python_version text,
    status text NOT NULL DEFAULT 'untested' CHECK (status IN (
        'tested', 'untested', 'forbidden', 'deprecated', 'planned'
    )),
    tested_at timestamptz,
    tested_by text,
    test_result jsonb,
    notes text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.compatibility_matrix_ext ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.compatibility_matrix_ext FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS compat_matrix_ext_read ON gptbridge_index.compatibility_matrix_ext;
CREATE POLICY compat_matrix_ext_read ON gptbridge_index.compatibility_matrix_ext
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS compat_matrix_ext_write ON gptbridge_index.compatibility_matrix_ext;
CREATE POLICY compat_matrix_ext_write ON gptbridge_index.compatibility_matrix_ext
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.compatibility_matrix_ext FROM PUBLIC;
GRANT SELECT ON gptbridge_index.compatibility_matrix_ext TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.compatibility_matrix_ext
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS compat_matrix_ext_release_idx
    ON gptbridge_index.compatibility_matrix_ext (release_id, status);
CREATE INDEX IF NOT EXISTS compat_matrix_ext_status_idx
    ON gptbridge_index.compatibility_matrix_ext (status);

-- ============================================================================
-- record_compatibility() — record a tested/forbidden combination
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_compatibility(
    p_release_id text,
    p_postgresql_version text,
    p_psycopg_version text,
    p_sqlite_runtime_version text,
    p_qdrant_server_version text,
    p_status text,
    p_tested_by text DEFAULT NULL,
    p_qdrant_client_version text DEFAULT NULL,
    p_python_version text DEFAULT NULL,
    p_test_result jsonb DEFAULT NULL,
    p_notes text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.compatibility_matrix_ext (
        release_id, postgresql_version, psycopg_version,
        sqlite_runtime_version, qdrant_server_version,
        qdrant_client_version, python_version,
        status, tested_at, tested_by, test_result, notes
    )
    VALUES (
        p_release_id, p_postgresql_version, p_psycopg_version,
        p_sqlite_runtime_version, p_qdrant_server_version,
        p_qdrant_client_version, p_python_version,
        p_status,
        CASE WHEN p_status = 'tested' THEN now() ELSE NULL END,
        p_tested_by, p_test_result, p_notes
    )
    RETURNING matrix_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- check_combination_allowed() — check if a combination is allowed
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.check_combination_allowed(
    p_postgresql_version text,
    p_psycopg_version text,
    p_sqlite_runtime_version text,
    p_qdrant_server_version text
) RETURNS text AS $$
DECLARE
    v_status text;
BEGIN
    SELECT status INTO v_status
    FROM gptbridge_index.compatibility_matrix_ext
    WHERE postgresql_version = p_postgresql_version
      AND psycopg_version = p_psycopg_version
      AND sqlite_runtime_version = p_sqlite_runtime_version
      AND qdrant_server_version = p_qdrant_server_version
    ORDER BY updated_at DESC
    LIMIT 1;

    IF v_status IS NULL THEN
        RETURN 'untested';
    END IF;
    RETURN v_status;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_forbidden_combinations() — list all forbidden combinations
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_forbidden_combinations()
RETURNS TABLE (
    postgresql_version text,
    psycopg_version text,
    sqlite_runtime_version text,
    qdrant_server_version text,
    notes text
) AS $$
BEGIN
    RETURN QUERY
    SELECT postgresql_version, psycopg_version,
           sqlite_runtime_version, qdrant_server_version, notes
    FROM gptbridge_index.compatibility_matrix_ext
    WHERE status = 'forbidden'
    ORDER BY updated_at DESC;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
