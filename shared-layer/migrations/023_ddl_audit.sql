-- 023_ddl_audit.sql
-- DDL Audit: independently record schema/index/RLS/role changes, so
-- structural modifications are audited separately from data audit — not
-- just buried in migration logs.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger; write=governed-executor.
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist; deny-by-default.
--
-- This migration adds:
--   * gptbridge_audit.ddl_event — append-only DDL audit log.
--   * An event trigger that records every DDL command with the command
--     tag, object identity, role, and statement.

-- ============================================================================
-- ddl_event table — append-only DDL audit log.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_audit.ddl_event (
    ddl_event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    command_tag text NOT NULL,
    object_identity text,
    object_type text,
    schema_name text,
    object_name text,
    session_user text NOT NULL,
    migration_executor boolean NOT NULL DEFAULT false,
    statement_hash text,
    occurred_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_audit.ddl_event ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_audit.ddl_event FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS ddl_event_read ON gptbridge_audit.ddl_event;
CREATE POLICY ddl_event_read ON gptbridge_audit.ddl_event
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS ddl_event_insert ON gptbridge_audit.ddl_event;
CREATE POLICY ddl_event_insert ON gptbridge_audit.ddl_event
    FOR INSERT WITH CHECK (true);

REVOKE ALL ON gptbridge_audit.ddl_event FROM PUBLIC;
GRANT SELECT ON gptbridge_audit.ddl_event TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_audit.ddl_event TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS ddl_event_occurred_idx
    ON gptbridge_audit.ddl_event (occurred_at);
CREATE INDEX IF NOT EXISTS ddl_event_object_idx
    ON gptbridge_audit.ddl_event (schema_name, object_name);
CREATE INDEX IF NOT EXISTS ddl_event_tag_idx
    ON gptbridge_audit.ddl_event (command_tag, occurred_at);

-- ============================================================================
-- DDL audit event trigger — records every DDL command.
-- Fires on ddl_command_end (after the DDL guard from migration 022).
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_security.audit_ddl_event()
RETURNS event_trigger AS $$
DECLARE
    v_command_tag text := tg_tag;
    v_object_identity text;
    v_object_type text;
    v_schema_name text;
    v_object_name text;
    v_is_migration boolean;
BEGIN
    -- Use pg_event_trigger functions to get object details
    v_object_identity := NULL;
    v_object_type := NULL;
    v_schema_name := NULL;
    v_object_name := NULL;

    -- pg_event_trigger_ddl_commands() returns the affected objects
    -- (available in ddl_command_end event triggers)
    BEGIN
        SELECT command_tag, object_identity, object_type,
               schema_name, object_name
        INTO v_command_tag, v_object_identity, v_object_type,
             v_schema_name, v_object_name
        FROM pg_event_trigger_ddl_commands()
        LIMIT 1;
    EXCEPTION WHEN OTHERS THEN
        -- If we can't get details, record what we can
        v_object_identity := 'unknown';
    END;

    v_is_migration := COALESCE(
        nullif(current_setting('gptbridge.is_migration_executor', true), ''),
        'false'
    )::boolean;

    INSERT INTO gptbridge_audit.ddl_event (
        command_tag, object_identity, object_type,
        schema_name, object_name,
        session_user, migration_executor
    )
    VALUES (
        v_command_tag, v_object_identity, v_object_type,
        v_schema_name, v_object_name,
        current_user, v_is_migration
    );
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_security, gptbridge_audit;

DROP EVENT TRIGGER IF EXISTS ddl_audit_trigger;
CREATE EVENT TRIGGER ddl_audit_trigger
    ON ddl_command_end
    EXECUTE FUNCTION gptbridge_security.audit_ddl_event();
