-- 074_version_lock.sql
-- Version Lock.
--
-- Lock engine/driver versions in the database release manifest:
--   PostgreSQL major/minor, psycopg, Python sqlite3 ↔ SQLite runtime,
--   Qdrant server/client, migration contract.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist.

-- ============================================================================
-- version_lock — locked engine/driver versions per release
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.version_lock (
    lock_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id text REFERENCES gptbridge_index.database_release(release_id),
    component text NOT NULL,  -- 'postgresql', 'psycopg', 'sqlite_runtime',
                              -- 'qdrant_server', 'qdrant_client',
                              -- 'migration_contract', 'python'
    major_version integer,
    minor_version integer,
    patch_version integer,
    version_string text NOT NULL,  -- full version string e.g. "18.3.0"
    locked_at timestamptz NOT NULL DEFAULT now(),
    locked_by text NOT NULL,
    description text
);

ALTER TABLE gptbridge_index.version_lock ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.version_lock FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS version_lock_read ON gptbridge_index.version_lock;
CREATE POLICY version_lock_read ON gptbridge_index.version_lock
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS version_lock_write ON gptbridge_index.version_lock;
CREATE POLICY version_lock_write ON gptbridge_index.version_lock
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.version_lock FROM PUBLIC;
GRANT SELECT ON gptbridge_index.version_lock TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.version_lock
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS version_lock_release_idx
    ON gptbridge_index.version_lock (release_id, component);
CREATE INDEX IF NOT EXISTS version_lock_component_idx
    ON gptbridge_index.version_lock (component, version_string);

-- ============================================================================
-- lock_version() — lock a component version for a release
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.lock_version(
    p_release_id text,
    p_component text,
    p_version_string text,
    p_major integer DEFAULT NULL,
    p_minor integer DEFAULT NULL,
    p_patch integer DEFAULT NULL,
    p_locked_by text,
    p_description text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.version_lock (
        release_id, component, major_version, minor_version, patch_version,
        version_string, locked_by, description
    )
    VALUES (
        p_release_id, p_component, p_major, p_minor, p_patch,
        p_version_string, p_locked_by, p_description
    )
    RETURNING lock_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_version_lock() — get locked version for a component in a release
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_version_lock(
    p_release_id text,
    p_component text
) RETURNS TABLE (
    version_string text,
    major_version integer,
    minor_version integer,
    patch_version integer
) AS $$
BEGIN
    RETURN QUERY
    SELECT version_string, major_version, minor_version, patch_version
    FROM gptbridge_index.version_lock
    WHERE release_id = p_release_id AND component = p_component
    ORDER BY locked_at DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_all_version_locks() — get all locked versions for a release
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_all_version_locks(
    p_release_id text
) RETURNS TABLE (
    component text,
    version_string text,
    major_version integer,
    minor_version integer,
    patch_version integer
) AS $$
BEGIN
    RETURN QUERY
    SELECT DISTINCT ON (component)
        component, version_string, major_version, minor_version, patch_version
    FROM gptbridge_index.version_lock
    WHERE release_id = p_release_id
    ORDER BY component, locked_at DESC;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
