-- 030_readonly_domain_startup_cert.sql
-- Read-Only Emergency Domain + Database Startup Certification.
--
-- Read-Only Domain: when integrity/schema drift/authority conflict occurs,
-- a specific data domain can be switched to read-only, rather than
-- shutting down the entire system.
--
-- Startup Certification: at startup, verify schema version, RLS, required
-- roles, migration head, audit append-only, and authority contract — not
-- just SELECT 1 — before marking the database READY.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist; deny-by-default.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- readonly_domain — per-domain read-only switch
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.readonly_domain (
    domain_name text PRIMARY KEY,
    is_readonly boolean NOT NULL DEFAULT false,
    reason text,
    activated_at timestamptz,
    activated_by text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.readonly_domain ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.readonly_domain FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS readonly_domain_read ON gptbridge_index.readonly_domain;
CREATE POLICY readonly_domain_read ON gptbridge_index.readonly_domain
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS readonly_domain_write ON gptbridge_index.readonly_domain;
CREATE POLICY readonly_domain_write ON gptbridge_index.readonly_domain
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.readonly_domain FROM PUBLIC;
GRANT SELECT ON gptbridge_index.readonly_domain TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.readonly_domain
    TO gptbridge_index_executor;

-- Seed default domains
INSERT INTO gptbridge_index.readonly_domain (domain_name, is_readonly) VALUES
    ('central-index'), ('transport'), ('audit'), ('rag'), ('governance')
ON CONFLICT (domain_name) DO NOTHING;

-- ============================================================================
-- set_domain_readonly() — switch a domain to read-only or read-write
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.set_domain_readonly(
    p_domain_name text,
    p_readonly boolean,
    p_reason text DEFAULT NULL,
    p_activated_by text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.readonly_domain
    SET is_readonly = p_readonly,
        reason = p_reason,
        activated_at = CASE WHEN p_readonly THEN now() ELSE activated_at END,
        activated_by = p_activated_by,
        updated_at = now()
    WHERE domain_name = p_domain_name;

    IF NOT FOUND THEN
        INSERT INTO gptbridge_index.readonly_domain (
            domain_name, is_readonly, reason, activated_at, activated_by
        )
        VALUES (
            p_domain_name, p_readonly, p_reason,
            CASE WHEN p_readonly THEN now() ELSE NULL END,
            p_activated_by
        );
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- is_domain_readonly() — check if a domain is currently read-only
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.is_domain_readonly(
    p_domain_name text
) RETURNS boolean AS $$
DECLARE
    v_readonly boolean;
BEGIN
    SELECT is_readonly INTO v_readonly
    FROM gptbridge_index.readonly_domain
    WHERE domain_name = p_domain_name;
    RETURN COALESCE(v_readonly, false);
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- startup_certification — records the startup certification result
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.startup_certification (
    certification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    schema_version_verified boolean NOT NULL DEFAULT false,
    rls_verified boolean NOT NULL DEFAULT false,
    required_roles_verified boolean NOT NULL DEFAULT false,
    migration_head_verified boolean NOT NULL DEFAULT false,
    audit_append_only_verified boolean NOT NULL DEFAULT false,
    authority_contract_verified boolean NOT NULL DEFAULT false,
    contract_version_verified boolean NOT NULL DEFAULT false,
    ready boolean NOT NULL DEFAULT false,
    checks jsonb NOT NULL DEFAULT '[]'::jsonb,
    certified_at timestamptz NOT NULL DEFAULT now(),
    certified_by text NOT NULL
);

ALTER TABLE gptbridge_index.startup_certification ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.startup_certification FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS startup_cert_read ON gptbridge_index.startup_certification;
CREATE POLICY startup_cert_read ON gptbridge_index.startup_certification
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS startup_cert_write ON gptbridge_index.startup_certification;
CREATE POLICY startup_cert_write ON gptbridge_index.startup_certification
    FOR INSERT WITH CHECK (true);

REVOKE ALL ON gptbridge_index.startup_certification FROM PUBLIC;
GRANT SELECT ON gptbridge_index.startup_certification TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_index.startup_certification TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS startup_cert_at_idx
    ON gptbridge_index.startup_certification (certified_at);

-- ============================================================================
-- record_startup_certification() — record the startup certification result
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_startup_certification(
    p_schema_version_verified boolean,
    p_rls_verified boolean,
    p_required_roles_verified boolean,
    p_migration_head_verified boolean,
    p_audit_append_only_verified boolean,
    p_authority_contract_verified boolean,
    p_contract_version_verified boolean,
    p_checks jsonb,
    p_certified_by text
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_ready boolean;
BEGIN
    v_ready := p_schema_version_verified
               AND p_rls_verified
               AND p_required_roles_verified
               AND p_migration_head_verified
               AND p_audit_append_only_verified
               AND p_authority_contract_verified
               AND p_contract_version_verified;
    INSERT INTO gptbridge_index.startup_certification (
        schema_version_verified, rls_verified, required_roles_verified,
        migration_head_verified, audit_append_only_verified,
        authority_contract_verified, contract_version_verified,
        ready, checks, certified_by
    )
    VALUES (
        p_schema_version_verified, p_rls_verified, p_required_roles_verified,
        p_migration_head_verified, p_audit_append_only_verified,
        p_authority_contract_verified, p_contract_version_verified,
        v_ready, p_checks, p_certified_by
    )
    RETURNING certification_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- is_database_ready() — check if the latest startup certification passed
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.is_database_ready()
RETURNS boolean AS $$
DECLARE
    v_ready boolean;
BEGIN
    SELECT ready INTO v_ready
    FROM gptbridge_index.startup_certification
    ORDER BY certified_at DESC
    LIMIT 1;
    RETURN COALESCE(v_ready, false);
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
