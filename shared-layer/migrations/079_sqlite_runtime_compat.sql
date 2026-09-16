-- 079_sqlite_runtime_compat.sql
-- SQLite Runtime Compatibility.
--
-- SQLite goes through Python stdlib. Record Python version ↔ SQLite library
-- version to detect FTS/WAL behavior changes after Python upgrade.
--
-- Codex basis:
--   A8/E21  — SQLite: owner-private-operational-state.
--   A44/E30 — four-functions-local.

-- ============================================================================
-- sqlite_runtime_compat — Python ↔ SQLite version compatibility tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.sqlite_runtime_compat (
    compat_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    python_version text NOT NULL,
    sqlite_library_version text NOT NULL,
    sqlite_source text,  -- 'stdlib', 'pysqlite3', 'custom'
    fts5_available boolean NOT NULL DEFAULT false,
    wal_mode_available boolean NOT NULL DEFAULT false,
    json1_available boolean NOT NULL DEFAULT false,
    tested_at timestamptz NOT NULL DEFAULT now(),
    tested_by text NOT NULL,
    test_result jsonb,
    notes text
);

ALTER TABLE gptbridge_index.sqlite_runtime_compat ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.sqlite_runtime_compat FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sqlite_rt_compat_read ON gptbridge_index.sqlite_runtime_compat;
CREATE POLICY sqlite_rt_compat_read ON gptbridge_index.sqlite_runtime_compat
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS sqlite_rt_compat_write ON gptbridge_index.sqlite_runtime_compat;
CREATE POLICY sqlite_rt_compat_write ON gptbridge_index.sqlite_runtime_compat
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.sqlite_runtime_compat FROM PUBLIC;
GRANT SELECT ON gptbridge_index.sqlite_runtime_compat TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.sqlite_runtime_compat
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS sqlite_rt_compat_py_idx
    ON gptbridge_index.sqlite_runtime_compat (python_version, sqlite_library_version);

-- ============================================================================
-- record_sqlite_runtime_compat() — record Python↔SQLite compatibility
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_sqlite_runtime_compat(
    p_python_version text,
    p_sqlite_library_version text,
    p_tested_by text,
    p_fts5_available boolean DEFAULT false,
    p_wal_mode_available boolean DEFAULT false,
    p_json1_available boolean DEFAULT false,
    p_sqlite_source text DEFAULT 'stdlib',
    p_test_result jsonb DEFAULT NULL,
    p_notes text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.sqlite_runtime_compat (
        python_version, sqlite_library_version, sqlite_source,
        fts5_available, wal_mode_available, json1_available,
        tested_by, test_result, notes
    )
    VALUES (
        p_python_version, p_sqlite_library_version, p_sqlite_source,
        p_fts5_available, p_wal_mode_available, p_json1_available,
        p_tested_by, p_test_result, p_notes
    )
    RETURNING compat_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- check_sqlite_runtime_compat() — check if Python+SQLite combo is tested
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.check_sqlite_runtime_compat(
    p_python_version text,
    p_sqlite_library_version text
) RETURNS boolean AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM gptbridge_index.sqlite_runtime_compat
        WHERE python_version = p_python_version
          AND sqlite_library_version = p_sqlite_library_version
          AND fts5_available = true
          AND wal_mode_available = true
          AND json1_available = true
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
