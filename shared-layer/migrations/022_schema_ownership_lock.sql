-- 022_schema_ownership_lock.sql
-- Schema Ownership Lock: each schema has a single owner role; runtime
-- roles cannot CREATE, ALTER, or DROP — DDL must go through the migration
-- executor only.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist; deny-by-default.
--   A46/E22 — Audit: mandatory-ledger; write=governed-executor.
--
-- This migration:
--   * Creates gptbridge_migration_owner NOLOGIN role (the sole DDL owner).
--   * Transfers ownership of all governed schemas to it.
--   * Creates an event trigger that rejects DDL from any non-owner role.

-- ============================================================================
-- Migration owner role — the only role allowed to perform DDL.
-- ============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_migration_owner') THEN
        CREATE ROLE gptbridge_migration_owner NOLOGIN;
    END IF;
END
$$;

-- ============================================================================
-- Transfer schema ownership to the migration owner.
-- ============================================================================
DO $$
DECLARE
    schema_record record;
BEGIN
    FOR schema_record IN
        SELECT schema_name FROM (
            VALUES
                ('gptbridge_index'),
                ('gptbridge_rag'),
                ('gptbridge_transport'),
                ('gptbridge_audit'),
                ('gptbridge_security'),
                ('registry')
        ) AS t(schema_name)
        WHERE EXISTS (
            SELECT 1 FROM information_schema.schemata
            WHERE schema_name = t.schema_name
        )
    LOOP
        EXECUTE format('ALTER SCHEMA %I OWNER TO gptbridge_migration_owner', schema_record.schema_name);
    END LOOP;
END
$$;

-- ============================================================================
-- DDL guard event trigger: reject DDL from non-migration-owner sessions.
-- The migration runner sets gptbridge.is_migration_executor = 'true' before
-- running DDL; any other session attempting DDL is rejected.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_security.ddl_guard()
RETURNS event_trigger AS $$
DECLARE
    v_is_migration boolean;
BEGIN
    -- Check if this session is authorized to run DDL
    v_is_migration := COALESCE(
        nullif(current_setting('gptbridge.is_migration_executor', true), ''),
        'false'
    )::boolean;

    IF NOT v_is_migration THEN
        RAISE EXCEPTION 'DDL_GUARD: DDL operations are only allowed via the migration executor (gptbridge_migration_owner). Set gptbridge.is_migration_executor=true to run DDL.';
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;

-- Event triggers fire on DDL commands; we guard the main DDL event.
DROP EVENT TRIGGER IF EXISTS ddl_guard_trigger;
CREATE EVENT TRIGGER ddl_guard_trigger
    ON ddl_command_end
    WHEN tag IN (
        'CREATE TABLE', 'ALTER TABLE', 'DROP TABLE',
        'CREATE INDEX', 'DROP INDEX',
        'CREATE SCHEMA', 'DROP SCHEMA',
        'ALTER SCHEMA',
        'CREATE FUNCTION', 'ALTER FUNCTION', 'DROP FUNCTION',
        'CREATE TRIGGER', 'DROP TRIGGER',
        'CREATE TYPE', 'ALTER TYPE', 'DROP TYPE',
        'CREATE VIEW', 'ALTER VIEW', 'DROP VIEW',
        'CREATE ROLE', 'ALTER ROLE', 'DROP ROLE',
        'CREATE POLICY', 'DROP POLICY',
        'GRANT', 'REVOKE'
    )
    EXECUTE FUNCTION gptbridge_security.ddl_guard();

-- Grant migration owner the right to set the flag
GRANT gptbridge_migration_owner TO gptbridge_index_executor;
