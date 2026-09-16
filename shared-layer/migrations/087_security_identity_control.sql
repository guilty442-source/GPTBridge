-- 087_security_identity_control.sql
-- Access control plane for the local data platform.
--
--  * gptbridge_security.credential         credential *metadata only*
--    (never plaintext: secret_id/owner/version/fingerprint/rotated_at/
--    expires_at/grace_until/status)
--  * gptbridge_security.security_generation generation ledger raised on
--    credential/role changes; sessions bound to an older generation must not
--    perform sensitive writes
--  * session identity helpers reading the transaction-local gptbridge.*
--    settings bound by shared_layer.security.session_identity
--  * least-privilege catalog view for release certification
--
-- Additive and idempotent; no runtime behaviour changes on apply.

CREATE TABLE IF NOT EXISTS gptbridge_security.credential (
    secret_id text PRIMARY KEY,
    owner text NOT NULL,
    role_name text NOT NULL DEFAULT '',
    version integer NOT NULL DEFAULT 1 CHECK (version >= 1),
    fingerprint text NOT NULL CHECK (length(fingerprint) BETWEEN 16 AND 128),
    status text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'grace', 'revoked', 'expired')),
    rotated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    grace_until timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE gptbridge_security.credential IS
    'Credential metadata only. Plaintext secrets are forbidden here; a fingerprint (HMAC-SHA256) is the maximum disclosure.';

CREATE TABLE IF NOT EXISTS gptbridge_security.security_generation (
    generation bigint PRIMARY KEY,
    reason text NOT NULL,
    actor_id text NOT NULL DEFAULT '',
    raised_at timestamptz NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION gptbridge_security.current_security_generation()
    RETURNS bigint
    LANGUAGE sql
    STABLE
AS $$
    SELECT COALESCE((SELECT max(generation) FROM gptbridge_security.security_generation), 0);
$$;

CREATE OR REPLACE FUNCTION gptbridge_security.raise_security_generation(
    p_reason text,
    p_actor text DEFAULT ''
)
    RETURNS bigint
    LANGUAGE plpgsql
AS $$
DECLARE
    next_generation bigint;
BEGIN
    IF p_reason IS NULL OR btrim(p_reason) = '' THEN
        RAISE EXCEPTION 'SECURITY_GENERATION_REASON_REQUIRED';
    END IF;
    SELECT gptbridge_security.current_security_generation() + 1 INTO next_generation;
    INSERT INTO gptbridge_security.security_generation(generation, reason, actor_id)
    VALUES (next_generation, p_reason, COALESCE(p_actor, ''));
    RETURN next_generation;
END;
$$;

CREATE OR REPLACE FUNCTION gptbridge_security.session_identity()
    RETURNS jsonb
    LANGUAGE sql
    STABLE
AS $$
    SELECT jsonb_build_object(
        'actor_id', current_setting('gptbridge.actor_id', true),
        'module_id', current_setting('gptbridge.module_id', true),
        'request_id', current_setting('gptbridge.request_id', true),
        'decision_id', current_setting('gptbridge.decision_id', true),
        'correlation_id', current_setting('gptbridge.correlation_id', true),
        'security_generation', current_setting('gptbridge.security_generation', true)
    );
$$;

CREATE OR REPLACE VIEW gptbridge_security.least_privilege_catalog AS
    SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls, rolcanlogin
    FROM pg_roles
    WHERE rolname LIKE 'gptbridge%'
    ORDER BY rolname;

REVOKE ALL ON gptbridge_security.credential FROM PUBLIC;
REVOKE ALL ON gptbridge_security.security_generation FROM PUBLIC;
REVOKE ALL ON gptbridge_security.least_privilege_catalog FROM PUBLIC;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_owner') THEN
        EXECUTE 'GRANT ALL ON gptbridge_security.credential TO gptbridge_owner';
        EXECUTE 'GRANT ALL ON gptbridge_security.security_generation TO gptbridge_owner';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_runtime') THEN
        EXECUTE 'GRANT SELECT, INSERT, UPDATE ON gptbridge_security.credential TO gptbridge_runtime';
        EXECUTE 'GRANT SELECT ON gptbridge_security.security_generation TO gptbridge_runtime';
    END IF;
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'gptbridge_xingcheng_reader') THEN
        EXECUTE 'GRANT SELECT ON gptbridge_security.credential TO gptbridge_xingcheng_reader';
        EXECUTE 'GRANT SELECT ON gptbridge_security.security_generation TO gptbridge_xingcheng_reader';
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS credential_status_idx
    ON gptbridge_security.credential (status, expires_at);
