-- 045_sqlite_template_release.sql
-- SQLite Template Release.
--
-- Each module's SQLite database must know:
--   template_version, schema_version, minimum_reader_version,
--   minimum_writer_version.
--
-- If an old module DB has template 5 and the new runtime requires >= 7,
-- it must migrate first — not open for write directly.
--
-- Codex basis:
--   A8/E21  — SQLite: owner-private-operational-state.
--   A44/E30 — four-functions-local.

-- ============================================================================
-- sqlite_template_release — versioned SQLite template releases
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.sqlite_template_release (
    template_version integer PRIMARY KEY,
    schema_version integer NOT NULL,
    minimum_reader_version integer NOT NULL DEFAULT 1,
    minimum_writer_version integer NOT NULL DEFAULT 1,
    ddl_hash text NOT NULL,  -- hash of the template DDL
    introduced_in_release text REFERENCES gptbridge_index.database_release(release_id),
    status text NOT NULL DEFAULT 'active' CHECK (status IN (
        'active', 'deprecated', 'retired'
    )),
    deprecated_at timestamptz,
    retired_at timestamptz,
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.sqlite_template_release ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.sqlite_template_release FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sqlite_tpl_read ON gptbridge_index.sqlite_template_release;
CREATE POLICY sqlite_tpl_read ON gptbridge_index.sqlite_template_release
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS sqlite_tpl_write ON gptbridge_index.sqlite_template_release;
CREATE POLICY sqlite_tpl_write ON gptbridge_index.sqlite_template_release
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.sqlite_template_release FROM PUBLIC;
GRANT SELECT ON gptbridge_index.sqlite_template_release TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.sqlite_template_release
    TO gptbridge_index_executor;

-- ============================================================================
-- register_sqlite_template() — register a new SQLite template version
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.register_sqlite_template(
    p_template_version integer,
    p_schema_version integer,
    p_ddl_hash text,
    p_minimum_reader_version integer DEFAULT 1,
    p_minimum_writer_version integer DEFAULT 1,
    p_description text DEFAULT NULL,
    p_introduced_in_release text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    INSERT INTO gptbridge_index.sqlite_template_release (
        template_version, schema_version, minimum_reader_version,
        minimum_writer_version, ddl_hash, description, introduced_in_release
    )
    VALUES (
        p_template_version, p_schema_version, p_minimum_reader_version,
        p_minimum_writer_version, p_ddl_hash, p_description, p_introduced_in_release
    )
    ON CONFLICT (template_version) DO UPDATE SET
        schema_version = EXCLUDED.schema_version,
        minimum_reader_version = EXCLUDED.minimum_reader_version,
        minimum_writer_version = EXCLUDED.minimum_writer_version,
        ddl_hash = EXCLUDED.ddl_hash,
        description = EXCLUDED.description,
        updated_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_active_sqlite_template() — get the active SQLite template version
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_active_sqlite_template()
RETURNS TABLE (
    template_version integer,
    schema_version integer,
    minimum_reader_version integer,
    minimum_writer_version integer,
    ddl_hash text
) AS $$
BEGIN
    RETURN QUERY
    SELECT template_version, schema_version,
           minimum_reader_version, minimum_writer_version, ddl_hash
    FROM gptbridge_index.sqlite_template_release
    WHERE status = 'active'
    ORDER BY template_version DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- can_write_sqlite() — check if a module's SQLite template can be written
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.can_write_sqlite(
    p_current_template_version integer
) RETURNS boolean AS $$
DECLARE
    v_required integer;
BEGIN
    SELECT minimum_writer_version INTO v_required
    FROM gptbridge_index.sqlite_template_release
    WHERE status = 'active'
    ORDER BY template_version DESC
    LIMIT 1;

    IF v_required IS NULL THEN
        RETURN true;  -- no template registered yet
    END IF;

    RETURN p_current_template_version >= v_required;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
