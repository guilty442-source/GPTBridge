-- 083_dependency_drift.sql
-- Dependency Drift Detection.
--
-- At startup, check:
--   expected version vs installed version
-- Mismatch → mark UNVERIFIED_DEPENDENCY
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A10/E10 — explicit-allowlist.

-- ============================================================================
-- dependency_drift — records version mismatches between expected and installed
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.dependency_drift (
    drift_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id text REFERENCES gptbridge_index.database_release(release_id),
    component text NOT NULL,
    expected_version text NOT NULL,
    installed_version text NOT NULL,
    drift_status text NOT NULL DEFAULT 'UNVERIFIED_DEPENDENCY' CHECK (drift_status IN (
        'VERIFIED', 'UNVERIFIED_DEPENDENCY', 'MISMATCH', 'DOWNGRADE',
        'UNREGISTERED', 'MISSING'
    )),
    detected_at timestamptz NOT NULL DEFAULT now(),
    detected_by text NOT NULL,
    resolved_at timestamptz,
    resolved_by text,
    resolution text
);

ALTER TABLE gptbridge_index.dependency_drift ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.dependency_drift FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS dep_drift_read ON gptbridge_index.dependency_drift;
CREATE POLICY dep_drift_read ON gptbridge_index.dependency_drift
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS dep_drift_write ON gptbridge_index.dependency_drift;
CREATE POLICY dep_drift_write ON gptbridge_index.dependency_drift
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.dependency_drift FROM PUBLIC;
GRANT SELECT ON gptbridge_index.dependency_drift TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.dependency_drift
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS dep_drift_status_idx
    ON gptbridge_index.dependency_drift (drift_status, detected_at);
CREATE INDEX IF NOT EXISTS dep_drift_component_idx
    ON gptbridge_index.dependency_drift (component, detected_at);

-- ============================================================================
-- record_dependency_drift() — record a version mismatch
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_dependency_drift(
    p_release_id text,
    p_component text,
    p_expected_version text,
    p_installed_version text,
    p_detected_by text
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_status text;
BEGIN
    -- Determine drift status
    IF p_installed_version IS NULL OR p_installed_version = '' THEN
        v_status := 'MISSING';
    ELSIF p_expected_version = p_installed_version THEN
        v_status := 'VERIFIED';
    ELSE
        v_status := 'MISMATCH';
    END IF;

    INSERT INTO gptbridge_index.dependency_drift (
        release_id, component, expected_version, installed_version,
        drift_status, detected_by
    )
    VALUES (
        p_release_id, p_component, p_expected_version, p_installed_version,
        v_status, p_detected_by
    )
    RETURNING drift_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- resolve_dependency_drift() — resolve a drift record
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.resolve_dependency_drift(
    p_drift_id uuid,
    p_resolved_by text,
    p_resolution text
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.dependency_drift
    SET drift_status = 'VERIFIED',
        resolved_at = now(),
        resolved_by = p_resolved_by,
        resolution = p_resolution
    WHERE drift_id = p_drift_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_unverified_dependencies() — find unresolved drift
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_unverified_dependencies()
RETURNS TABLE (
    drift_id uuid,
    component text,
    expected_version text,
    installed_version text,
    drift_status text,
    detected_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT drift_id, component, expected_version, installed_version,
           drift_status, detected_at
    FROM gptbridge_index.dependency_drift
    WHERE resolved_at IS NULL
      AND drift_status != 'VERIFIED'
    ORDER BY detected_at DESC;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
