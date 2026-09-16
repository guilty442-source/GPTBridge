-- 118_schema_readiness.sql
-- Per-Schema Readiness.
--
-- Each PostgreSQL schema has its own readiness flag:
--   index_ready, transport_ready, audit_ready, rag_metadata_ready, identity_ready
-- Not a single postgres_ready = true for all.
--
-- Audit must be writable BEFORE business writes enabled.
-- Gate: authority_ready AND audit_ready AND security_ready
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.schema_readiness (
    readiness_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    schema_name text NOT NULL UNIQUE CHECK (schema_name IN (
        'gptbridge_index', 'gptbridge_transport', 'gptbridge_audit',
        'gptbridge_rag', 'gptbridge_identity', 'gptbridge_security'
    )),
    ready boolean NOT NULL DEFAULT false,
    checked_at timestamptz,
    schema_version integer,
    migration_head integer,
    rls_enabled boolean NOT NULL DEFAULT false,
    force_rls boolean NOT NULL DEFAULT false,
    required_roles_present boolean NOT NULL DEFAULT false,
    public_grants_revoked boolean NOT NULL DEFAULT false,
    security_generation integer,
    failure_reason text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.schema_readiness ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.schema_readiness FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS schema_readiness_read ON gptbridge_index.schema_readiness;
CREATE POLICY schema_readiness_read ON gptbridge_index.schema_readiness
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS schema_readiness_write ON gptbridge_index.schema_readiness;
CREATE POLICY schema_readiness_write ON gptbridge_index.schema_readiness
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.schema_readiness FROM PUBLIC;
GRANT SELECT ON gptbridge_index.schema_readiness TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.schema_readiness
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS schema_readiness_ready_idx
    ON gptbridge_index.schema_readiness (ready, schema_name);

CREATE OR REPLACE FUNCTION gptbridge_index.set_schema_readiness(
    p_schema_name text,
    p_ready boolean,
    p_schema_version integer DEFAULT NULL,
    p_migration_head integer DEFAULT NULL,
    p_rls_enabled boolean DEFAULT NULL,
    p_force_rls boolean DEFAULT NULL,
    p_required_roles boolean DEFAULT NULL,
    p_public_grants_revoked boolean DEFAULT NULL,
    p_security_generation integer DEFAULT NULL,
    p_failure_reason text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    INSERT INTO gptbridge_index.schema_readiness (
        schema_name, ready, checked_at, schema_version, migration_head,
        rls_enabled, force_rls, required_roles_present,
        public_grants_revoked, security_generation, failure_reason
    )
    VALUES (
        p_schema_name, p_ready, now(), p_schema_version, p_migration_head,
        COALESCE(p_rls_enabled, false), COALESCE(p_force_rls, false),
        COALESCE(p_required_roles, false), COALESCE(p_public_grants_revoked, false),
        p_security_generation, p_failure_reason
    )
    ON CONFLICT (schema_name) DO UPDATE SET
        ready = EXCLUDED.ready,
        checked_at = now(),
        schema_version = COALESCE(EXCLUDED.schema_version, schema_readiness.schema_version),
        migration_head = COALESCE(EXCLUDED.migration_head, schema_readiness.migration_head),
        rls_enabled = COALESCE(EXCLUDED.rls_enabled, schema_readiness.rls_enabled),
        force_rls = COALESCE(EXCLUDED.force_rls, schema_readiness.force_rls),
        required_roles_present = COALESCE(EXCLUDED.required_roles_present, schema_readiness.required_roles_present),
        public_grants_revoked = COALESCE(EXCLUDED.public_grants_revoked, schema_readiness.public_grants_revoked),
        security_generation = COALESCE(EXCLUDED.security_generation, schema_readiness.security_generation),
        failure_reason = p_failure_reason,
        updated_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.is_schema_ready(
    p_schema_name text
) RETURNS boolean AS $$
BEGIN
    RETURN COALESCE(
        (SELECT ready FROM gptbridge_index.schema_readiness
         WHERE schema_name = p_schema_name),
        false
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.is_pg_certified()
RETURNS boolean AS $$
BEGIN
    -- PG is certified only when ALL required schemas are ready
    RETURN NOT EXISTS (
        SELECT 1 FROM gptbridge_index.schema_readiness
        WHERE schema_name IN (
            'gptbridge_index', 'gptbridge_transport', 'gptbridge_audit',
            'gptbridge_rag', 'gptbridge_identity'
        )
        AND ready = false
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.is_audit_writable()
RETURNS boolean AS $$
BEGIN
    RETURN is_schema_ready('gptbridge_audit');
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.can_enable_business_write()
RETURNS boolean AS $$
BEGIN
    -- Business write requires: authority_ready AND audit_ready AND security_ready
    RETURN is_pg_certified()
       AND is_schema_ready('gptbridge_audit')
       AND is_schema_ready('gptbridge_security');
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
