-- 044_rls_role_migration.sql
-- RLS / Role Migration Tracking.
--
-- GRANT, REVOKE, CREATE ROLE, ALTER POLICY, DROP POLICY, SECURITY DEFINER
-- function changes must go through version control, testing, and
-- certification — just like table migrations.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist; deny-by-default.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- rls_role_migration — tracks RLS/Role changes as versioned migrations
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.rls_role_migration (
    migration_id integer GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    rls_role_version integer NOT NULL,  -- RLS or Role version number
    change_type text NOT NULL CHECK (change_type IN (
        'create_role', 'alter_role', 'drop_role',
        'grant', 'revoke',
        'create_policy', 'alter_policy', 'drop_policy',
        'security_definer_grant', 'security_definer_revoke'
    )),
    target_object text NOT NULL,  -- role name or table/schema.policy
    change_sql text NOT NULL,
    rollback_sql text,
    introduced_in_release text REFERENCES gptbridge_index.database_release(release_id),
    applied_at timestamptz NOT NULL DEFAULT now(),
    applied_by text NOT NULL
);

ALTER TABLE gptbridge_index.rls_role_migration ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.rls_role_migration FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS rls_role_mig_read ON gptbridge_index.rls_role_migration;
CREATE POLICY rls_role_mig_read ON gptbridge_index.rls_role_migration
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS rls_role_mig_write ON gptbridge_index.rls_role_migration;
CREATE POLICY rls_role_mig_write ON gptbridge_index.rls_role_migration
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.rls_role_migration FROM PUBLIC;
GRANT SELECT ON gptbridge_index.rls_role_migration TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.rls_role_migration
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS rls_role_mig_version_idx
    ON gptbridge_index.rls_role_migration (rls_role_version);
CREATE INDEX IF NOT EXISTS rls_role_mig_type_idx
    ON gptbridge_index.rls_role_migration (change_type, applied_at);

-- ============================================================================
-- record_rls_role_migration() — record an RLS/Role change
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_rls_role_migration(
    p_rls_role_version integer,
    p_change_type text,
    p_target_object text,
    p_change_sql text,
    p_applied_by text,
    p_rollback_sql text DEFAULT NULL,
    p_introduced_in_release text DEFAULT NULL
) RETURNS integer AS $$
DECLARE
    v_id integer;
BEGIN
    INSERT INTO gptbridge_index.rls_role_migration (
        rls_role_version, change_type, target_object,
        change_sql, rollback_sql, introduced_in_release, applied_by
    )
    VALUES (
        p_rls_role_version, p_change_type, p_target_object,
        p_change_sql, p_rollback_sql, p_introduced_in_release, p_applied_by
    )
    RETURNING migration_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_rls_role_version() — get the latest RLS/Role version
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_rls_role_version()
RETURNS integer AS $$
DECLARE
    v_version integer;
BEGIN
    SELECT MAX(rls_role_version) INTO v_version
    FROM gptbridge_index.rls_role_migration;
    RETURN COALESCE(v_version, 0);
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
