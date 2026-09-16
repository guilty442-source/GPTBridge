-- 084_offline_bundle.sql
-- Offline Installation Bundle.
--
-- Local-only direction: keep verified installers/wheels/binaries with hashes:
--   PostgreSQL installer/package, psycopg wheel, Qdrant binary,
--   Python dependency wheels
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- offline_bundle — verified offline installation packages
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.offline_bundle (
    bundle_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id text REFERENCES gptbridge_index.database_release(release_id),
    component text NOT NULL,  -- 'postgresql', 'psycopg', 'qdrant', 'python_wheel'
    version text NOT NULL,
    package_type text NOT NULL CHECK (package_type IN (
        'installer', 'wheel', 'binary', 'archive', 'package'
    )),
    platform text,  -- 'windows', 'linux', 'macos', 'universal'
    storage_locator text NOT NULL,  -- opaque locator
    file_hash text NOT NULL,  -- SHA-256 of the file
    file_size_bytes bigint,
    hash_algorithm text NOT NULL DEFAULT 'sha256',
    verified boolean NOT NULL DEFAULT false,
    verified_at timestamptz,
    verified_by text,
    created_at timestamptz NOT NULL DEFAULT now(),
    notes text
);

ALTER TABLE gptbridge_index.offline_bundle ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.offline_bundle FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS offline_bundle_read ON gptbridge_index.offline_bundle;
CREATE POLICY offline_bundle_read ON gptbridge_index.offline_bundle
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS offline_bundle_write ON gptbridge_index.offline_bundle;
CREATE POLICY offline_bundle_write ON gptbridge_index.offline_bundle
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.offline_bundle FROM PUBLIC;
GRANT SELECT ON gptbridge_index.offline_bundle TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.offline_bundle
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS offline_bundle_release_idx
    ON gptbridge_index.offline_bundle (release_id, component);
CREATE INDEX IF NOT EXISTS offline_bundle_component_idx
    ON gptbridge_index.offline_bundle (component, version, platform);

-- ============================================================================
-- register_offline_bundle() — register an offline package
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.register_offline_bundle(
    p_release_id text,
    p_component text,
    p_version text,
    p_package_type text,
    p_storage_locator text,
    p_file_hash text,
    p_platform text DEFAULT NULL,
    p_file_size_bytes bigint DEFAULT NULL,
    p_hash_algorithm text DEFAULT 'sha256',
    p_notes text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.offline_bundle (
        release_id, component, version, package_type,
        platform, storage_locator, file_hash, file_size_bytes,
        hash_algorithm, notes
    )
    VALUES (
        p_release_id, p_component, p_version, p_package_type,
        p_platform, p_storage_locator, p_file_hash, p_file_size_bytes,
        p_hash_algorithm, p_notes
    )
    RETURNING bundle_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- verify_offline_bundle() — verify an offline package hash
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.verify_offline_bundle(
    p_bundle_id uuid,
    p_verified_by text
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.offline_bundle
    SET verified = true, verified_at = now(), verified_by = p_verified_by
    WHERE bundle_id = p_bundle_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_offline_bundle() — find a verified bundle for a component+version
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_offline_bundle(
    p_component text,
    p_version text,
    p_platform text DEFAULT NULL
) RETURNS TABLE (
    bundle_id uuid,
    storage_locator text,
    file_hash text,
    verified boolean
) AS $$
BEGIN
    RETURN QUERY
    SELECT bundle_id, storage_locator, file_hash, verified
    FROM gptbridge_index.offline_bundle
    WHERE component = p_component
      AND version = p_version
      AND (p_platform IS NULL OR platform = p_platform OR platform = 'universal')
    ORDER BY verified DESC, created_at DESC;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
