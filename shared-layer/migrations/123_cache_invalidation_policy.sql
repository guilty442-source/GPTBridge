-- 123_cache_invalidation_policy.sql
-- Cache Invalidation Policy.
--
-- Cache startup checks:
--   generation compatible?
--   revision compatible?
--   TTL valid?
-- If any fail → invalidate (don't risk consistency to preserve cache).
--
-- Codex basis:
--   A10/E10 — explicit-allowlist.
--   A8/E21  — PostgreSQL: central-structured-official-data.

CREATE TABLE IF NOT EXISTS gptbridge_index.cache_invalidation_policy (
    policy_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    cache_name text NOT NULL,
    check_generation_compatible boolean NOT NULL DEFAULT true,
    check_revision_compatible boolean NOT NULL DEFAULT true,
    check_ttl_valid boolean NOT NULL DEFAULT true,
    ttl_seconds integer,
    on_mismatch text NOT NULL DEFAULT 'invalidate' CHECK (on_mismatch IN (
        'invalidate', 'rebuild', 'degrade'
    )),
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.cache_invalidation_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.cache_invalidation_policy FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS cache_inval_read ON gptbridge_index.cache_invalidation_policy;
CREATE POLICY cache_inval_read ON gptbridge_index.cache_invalidation_policy
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS cache_inval_write ON gptbridge_index.cache_invalidation_policy;
CREATE POLICY cache_inval_write ON gptbridge_index.cache_invalidation_policy
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.cache_invalidation_policy FROM PUBLIC;
GRANT SELECT ON gptbridge_index.cache_invalidation_policy TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.cache_invalidation_policy
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS cache_inval_name_idx
    ON gptbridge_index.cache_invalidation_policy (cache_name);

CREATE OR REPLACE FUNCTION gptbridge_index.register_cache_policy(
    p_cache_name text,
    p_ttl_seconds integer DEFAULT NULL,
    p_on_mismatch text DEFAULT 'invalidate',
    p_check_gen boolean DEFAULT true,
    p_check_rev boolean DEFAULT true,
    p_check_ttl boolean DEFAULT true
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.cache_invalidation_policy (
        cache_name, check_generation_compatible, check_revision_compatible,
        check_ttl_valid, ttl_seconds, on_mismatch
    )
    VALUES (
        p_cache_name, p_check_gen, p_check_rev,
        p_check_ttl, p_ttl_seconds, p_on_mismatch
    )
    ON CONFLICT DO NOTHING
    RETURNING policy_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.should_invalidate_cache(
    p_cache_name text,
    p_generation_compatible boolean,
    p_revision_compatible boolean,
    p_ttl_valid boolean
) RETURNS boolean AS $$
DECLARE
    v_policy record;
BEGIN
    SELECT * INTO v_policy
    FROM gptbridge_index.cache_invalidation_policy
    WHERE cache_name = p_cache_name;

    IF NOT FOUND THEN
        -- No policy → invalidate by default (safe)
        RETURN true;
    END IF;

    IF v_policy.check_generation_compatible AND NOT p_generation_compatible THEN
        RETURN true;
    END IF;

    IF v_policy.check_revision_compatible AND NOT p_revision_compatible THEN
        RETURN true;
    END IF;

    IF v_policy.check_ttl_valid AND NOT p_ttl_valid THEN
        RETURN true;
    END IF;

    RETURN false;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
