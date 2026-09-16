-- 024_contract_version_handshake.sql
-- Data Contract Version Handshake: modules declare their supported
-- contract_version when connecting; incompatible versions are rejected
-- from writing, rather than discovering incompatibility via errors.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist; deny-by-default.
--   A44/E30 — four-functions-local; UNAVAILABLE:declare-closed-not-replace.
--
-- This migration adds:
--   * gptbridge_index.contract_version — declares each contract name,
--     its current version, and the minimum compatible version.
--   * A trigger that checks the session's declared contract_version
--     before allowing writes; if the declared version is below
--     min_compatible, the write is rejected.

-- ============================================================================
-- contract_version table
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.contract_version (
    contract_name text PRIMARY KEY,
    current_version text NOT NULL,
    min_compatible_version text NOT NULL,
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.contract_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.contract_version FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS contract_version_read ON gptbridge_index.contract_version;
CREATE POLICY contract_version_read ON gptbridge_index.contract_version
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS contract_version_write ON gptbridge_index.contract_version;
CREATE POLICY contract_version_write ON gptbridge_index.contract_version
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.contract_version FROM PUBLIC;
GRANT SELECT ON gptbridge_index.contract_version TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.contract_version
    TO gptbridge_index_executor;

-- Seed the central index contract version
INSERT INTO gptbridge_index.contract_version (
    contract_name, current_version, min_compatible_version, description
) VALUES (
    'central-index',
    '2026-09-16',
    '2026-09-01',
    'Central resource index + RAG + transport + audit schema contract'
) ON CONFLICT (contract_name) DO NOTHING;

-- ============================================================================
-- Contract version comparison helper.
-- Returns true if declared_version >= min_compatible_version.
-- Uses simple string comparison (ISO date format: YYYY-MM-DD).
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.check_contract_compatibility(
    p_contract_name text,
    p_declared_version text
) RETURNS boolean AS $$
DECLARE
    v_min_compatible text;
BEGIN
    SELECT min_compatible_version INTO v_min_compatible
    FROM gptbridge_index.contract_version
    WHERE contract_name = p_contract_name;

    IF v_min_compatible IS NULL THEN
        -- Unknown contract — allow (fail-open for unknown contracts;
        -- the contract_version table is the allowlist, not a denylist)
        RETURN true;
    END IF;

    IF p_declared_version IS NULL OR p_declared_version = '' THEN
        -- No version declared — reject (fail-closed for known contracts)
        RETURN false;
    END IF;

    RETURN p_declared_version >= v_min_compatible;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- Contract version fence trigger: reject writes from sessions that
-- declared an incompatible contract version.
-- The runtime sets gptbridge.contract_version before writing.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.enforce_contract_version()
RETURNS trigger AS $$
DECLARE
    v_declared_version text;
    v_compatible boolean;
BEGIN
    v_declared_version := nullif(current_setting('gptbridge.contract_version', true), '');

    IF v_declared_version IS NOT NULL THEN
        v_compatible := gptbridge_index.check_contract_compatibility(
            'central-index', v_declared_version
        );
        IF NOT v_compatible THEN
            RAISE EXCEPTION 'CONTRACT_VERSION_FENCE: declared version % is below minimum compatible version for central-index; reconnect with a compatible contract_version',
                v_declared_version;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- Apply the contract fence to resource writes
DROP TRIGGER IF EXISTS resource_contract_version_fence ON gptbridge_index.resource;
CREATE TRIGGER resource_contract_version_fence
    BEFORE INSERT OR UPDATE ON gptbridge_index.resource
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_index.enforce_contract_version();

-- Apply the contract fence to index_state writes
DROP TRIGGER IF EXISTS index_state_contract_version_fence ON gptbridge_rag.index_state;
CREATE TRIGGER index_state_contract_version_fence
    BEFORE INSERT OR UPDATE ON gptbridge_rag.index_state
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_index.enforce_contract_version();
