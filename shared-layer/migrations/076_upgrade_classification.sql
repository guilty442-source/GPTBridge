-- 076_upgrade_classification.sql
-- Upgrade Classification.
--
-- patch / minor / major
--   patch  → quick validation
--   minor  → targeted tests
--   major  → full restore/certification flow
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist.

-- ============================================================================
-- upgrade_classification — classify upgrades by scope
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.upgrade_classification (
    classification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    component text NOT NULL,  -- 'postgresql', 'psycopg', 'sqlite', 'qdrant'
    from_version text NOT NULL,
    to_version text NOT NULL,
    upgrade_class text NOT NULL CHECK (upgrade_class IN (
        'patch', 'minor', 'major'
    )),
    required_validation text NOT NULL DEFAULT 'standard' CHECK (required_validation IN (
        'quick', 'standard', 'full_restore_certification'
    )),
    allows_unattended boolean NOT NULL DEFAULT false,
    requires_backup boolean NOT NULL DEFAULT false,
    requires_clone_test boolean NOT NULL DEFAULT false,
    requires_certification boolean NOT NULL DEFAULT false,
    rollback_allowed boolean NOT NULL DEFAULT true,
    description text,
    classified_by text NOT NULL,
    classified_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.upgrade_classification ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.upgrade_classification FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS upgrade_class_read ON gptbridge_index.upgrade_classification;
CREATE POLICY upgrade_class_read ON gptbridge_index.upgrade_classification
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS upgrade_class_write ON gptbridge_index.upgrade_classification;
CREATE POLICY upgrade_class_write ON gptbridge_index.upgrade_classification
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.upgrade_classification FROM PUBLIC;
GRANT SELECT ON gptbridge_index.upgrade_classification TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.upgrade_classification
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS upgrade_class_component_idx
    ON gptbridge_index.upgrade_classification (component, from_version, to_version);
CREATE INDEX IF NOT EXISTS upgrade_class_class_idx
    ON gptbridge_index.upgrade_classification (upgrade_class);

-- ============================================================================
-- classify_upgrade() — classify an upgrade between two versions
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.classify_upgrade(
    p_component text,
    p_from_version text,
    p_to_version text,
    p_class text,
    p_classified_by text,
    p_description text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_validation text;
    v_unattended boolean;
    v_backup boolean;
    v_clone boolean;
    v_cert boolean;
    v_rollback boolean;
BEGIN
    -- Set defaults based on class
    CASE p_class
        WHEN 'patch' THEN
            v_validation := 'quick';
            v_unattended := true;
            v_backup := false;
            v_clone := false;
            v_cert := false;
            v_rollback := true;
        WHEN 'minor' THEN
            v_validation := 'standard';
            v_unattended := false;
            v_backup := true;
            v_clone := false;
            v_cert := false;
            v_rollback := true;
        WHEN 'major' THEN
            v_validation := 'full_restore_certification';
            v_unattended := false;
            v_backup := true;
            v_clone := true;
            v_cert := true;
            v_rollback := false;  -- roll-forward preferred for major
    END CASE;

    INSERT INTO gptbridge_index.upgrade_classification (
        component, from_version, to_version, upgrade_class,
        required_validation, allows_unattended, requires_backup,
        requires_clone_test, requires_certification, rollback_allowed,
        description, classified_by
    )
    VALUES (
        p_component, p_from_version, p_to_version, p_class,
        v_validation, v_unattended, v_backup,
        v_clone, v_cert, v_rollback,
        p_description, p_classified_by
    )
    RETURNING classification_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_upgrade_class() — get the classification for an upgrade
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_upgrade_class(
    p_component text,
    p_from_version text,
    p_to_version text
) RETURNS TABLE (
    upgrade_class text,
    required_validation text,
    allows_unattended boolean,
    requires_backup boolean,
    requires_clone_test boolean,
    requires_certification boolean,
    rollback_allowed boolean
) AS $$
BEGIN
    RETURN QUERY
    SELECT upgrade_class, required_validation, allows_unattended,
           requires_backup, requires_clone_test, requires_certification,
           rollback_allowed
    FROM gptbridge_index.upgrade_classification
    WHERE component = p_component
      AND from_version = p_from_version
      AND to_version = p_to_version
    ORDER BY classified_at DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
