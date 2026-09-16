-- 066_resource_content_hash.sql
-- Resource Content Hash.
--
-- Central index stores:
--   resource_hash, metadata_hash, locator_hash, revision
--
-- But does NOT store original content in PostgreSQL.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist.

-- ============================================================================
-- Add content hash columns to gptbridge_index.resource
-- ============================================================================
ALTER TABLE gptbridge_index.resource
    ADD COLUMN IF NOT EXISTS resource_hash text;
ALTER TABLE gptbridge_index.resource
    ADD COLUMN IF NOT EXISTS metadata_hash text;
ALTER TABLE gptbridge_index.resource
    ADD COLUMN IF NOT EXISTS locator_hash text;

CREATE INDEX IF NOT EXISTS resource_hash_idx
    ON gptbridge_index.resource (resource_hash);
CREATE INDEX IF NOT EXISTS resource_metadata_hash_idx
    ON gptbridge_index.resource (metadata_hash);

-- ============================================================================
-- resource_content_hash — separate table for hash tracking
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.resource_content_hash (
    resource_id text PRIMARY KEY,
    resource_hash text NOT NULL,
    metadata_hash text,
    locator_hash text,
    revision integer NOT NULL DEFAULT 0,
    hash_algorithm text NOT NULL DEFAULT 'sha256',
    computed_at timestamptz NOT NULL DEFAULT now(),
    verified_at timestamptz,
    tamper_state text NOT NULL DEFAULT 'unverified' CHECK (tamper_state IN (
        'verified', 'unverified', 'mismatch', 'tampered',
        'incomplete', 'rebuild_required'
    ))
);

ALTER TABLE gptbridge_index.resource_content_hash ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.resource_content_hash FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS resource_hash_read ON gptbridge_index.resource_content_hash;
CREATE POLICY resource_hash_read ON gptbridge_index.resource_content_hash
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS resource_hash_write ON gptbridge_index.resource_content_hash;
CREATE POLICY resource_hash_write ON gptbridge_index.resource_content_hash
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.resource_content_hash FROM PUBLIC;
GRANT SELECT ON gptbridge_index.resource_content_hash TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.resource_content_hash
    TO gptbridge_index_executor;

-- ============================================================================
-- record_resource_hash() — record or update a resource content hash
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_resource_hash(
    p_resource_id text,
    p_resource_hash text,
    p_revision integer,
    p_metadata_hash text DEFAULT NULL,
    p_locator_hash text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    INSERT INTO gptbridge_index.resource_content_hash (
        resource_id, resource_hash, metadata_hash, locator_hash, revision
    )
    VALUES (p_resource_id, p_resource_hash, p_metadata_hash,
            p_locator_hash, p_revision)
    ON CONFLICT (resource_id) DO UPDATE SET
        resource_hash = EXCLUDED.resource_hash,
        metadata_hash = EXCLUDED.metadata_hash,
        locator_hash = EXCLUDED.locator_hash,
        revision = EXCLUDED.revision,
        computed_at = now(),
        tamper_state = 'unverified';
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- verify_resource_hash() — verify a resource hash matches expected
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.verify_resource_hash(
    p_resource_id text,
    p_expected_hash text
) RETURNS boolean AS $$
DECLARE
    v_actual text;
BEGIN
    SELECT resource_hash INTO v_actual
    FROM gptbridge_index.resource_content_hash
    WHERE resource_id = p_resource_id;

    IF v_actual IS NULL THEN
        UPDATE gptbridge_index.resource_content_hash
        SET tamper_state = 'incomplete'
        WHERE resource_id = p_resource_id;
        RETURN false;
    END IF;

    IF v_actual = p_expected_hash THEN
        UPDATE gptbridge_index.resource_content_hash
        SET tamper_state = 'verified', verified_at = now()
        WHERE resource_id = p_resource_id;
        RETURN true;
    ELSE
        UPDATE gptbridge_index.resource_content_hash
        SET tamper_state = 'mismatch'
        WHERE resource_id = p_resource_id;
        RETURN false;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_tampered_resources() — find resources with tamper issues
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_tampered_resources(
    p_limit integer DEFAULT 100
) RETURNS TABLE (
    resource_id text,
    tamper_state text,
    revision integer,
    computed_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT resource_id, tamper_state, revision, computed_at
    FROM gptbridge_index.resource_content_hash
    WHERE tamper_state IN ('mismatch', 'tampered', 'incomplete', 'rebuild_required')
    ORDER BY computed_at DESC
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
