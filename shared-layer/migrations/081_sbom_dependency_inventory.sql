-- 081_sbom_dependency_inventory.sql
-- SBOM / Dependency Inventory.
--
-- Record at minimum:
--   component, version, source, hash, install_path, release_id, verified_at
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- sbom_dependency_inventory — software bill of materials
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.sbom_dependency_inventory (
    sbom_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id text REFERENCES gptbridge_index.database_release(release_id),
    component text NOT NULL,  -- e.g. 'postgresql', 'psycopg', 'qdrant'
    component_type text NOT NULL CHECK (component_type IN (
        'engine', 'driver', 'library', 'tool', 'runtime', 'package'
    )),
    version text NOT NULL,
    source text,  -- URL or package source
    source_hash text,  -- hash of the source/distribution
    install_path text,
    license_name text,
    verified boolean NOT NULL DEFAULT false,
    verified_at timestamptz,
    verified_by text,
    notes text,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.sbom_dependency_inventory ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.sbom_dependency_inventory FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sbom_read ON gptbridge_index.sbom_dependency_inventory;
CREATE POLICY sbom_read ON gptbridge_index.sbom_dependency_inventory
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS sbom_write ON gptbridge_index.sbom_dependency_inventory;
CREATE POLICY sbom_write ON gptbridge_index.sbom_dependency_inventory
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.sbom_dependency_inventory FROM PUBLIC;
GRANT SELECT ON gptbridge_index.sbom_dependency_inventory TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.sbom_dependency_inventory
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS sbom_release_idx
    ON gptbridge_index.sbom_dependency_inventory (release_id, component);
CREATE INDEX IF NOT EXISTS sbom_component_idx
    ON gptbridge_index.sbom_dependency_inventory (component, version);
CREATE INDEX IF NOT EXISTS sbom_verified_idx
    ON gptbridge_index.sbom_dependency_inventory (verified);

-- ============================================================================
-- record_sbom_entry() — record a SBOM entry
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_sbom_entry(
    p_release_id text,
    p_component text,
    p_component_type text,
    p_version text,
    p_source text DEFAULT NULL,
    p_source_hash text DEFAULT NULL,
    p_install_path text DEFAULT NULL,
    p_license_name text DEFAULT NULL,
    p_notes text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.sbom_dependency_inventory (
        release_id, component, component_type, version,
        source, source_hash, install_path, license_name, notes
    )
    VALUES (
        p_release_id, p_component, p_component_type, p_version,
        p_source, p_source_hash, p_install_path, p_license_name, p_notes
    )
    RETURNING sbom_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- verify_sbom_entry() — mark an SBOM entry as verified
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.verify_sbom_entry(
    p_sbom_id uuid,
    p_verified_by text
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.sbom_dependency_inventory
    SET verified = true, verified_at = now(), verified_by = p_verified_by
    WHERE sbom_id = p_sbom_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_sbom_for_release() — get full SBOM for a release
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_sbom_for_release(
    p_release_id text
) RETURNS TABLE (
    component text,
    component_type text,
    version text,
    source text,
    source_hash text,
    install_path text,
    verified boolean
) AS $$
BEGIN
    RETURN QUERY
    SELECT component, component_type, version, source,
           source_hash, install_path, verified
    FROM gptbridge_index.sbom_dependency_inventory
    WHERE release_id = p_release_id
    ORDER BY component_type, component;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
