-- 042_migration_breaking_change.sql
-- Migration Breaking Change Classification.
--
-- Migrations are classified as:
--   compatible  — nullable column add, new index, new table
--   conditional  — column type change, data backfill
--   breaking     — drop table, rename contract, remove column
--
-- Breaking migrations must have:
--   pre_migration, data_transform, compatibility_window,
--   post_migration, rollback/recovery plan
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- migration_classification — classify each migration
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.migration_classification (
    migration_id integer PRIMARY KEY,
    migration_name text NOT NULL,
    change_type text NOT NULL CHECK (change_type IN (
        'compatible', 'conditional', 'breaking'
    )),
    pre_migration text,
    data_transform text,
    compatibility_window_days integer,
    post_migration text,
    rollback_plan text,
    recovery_plan text,
    classified_at timestamptz NOT NULL DEFAULT now(),
    classified_by text NOT NULL
);

ALTER TABLE gptbridge_index.migration_classification ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.migration_classification FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS migration_class_read ON gptbridge_index.migration_classification;
CREATE POLICY migration_class_read ON gptbridge_index.migration_classification
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS migration_class_write ON gptbridge_index.migration_classification;
CREATE POLICY migration_class_write ON gptbridge_index.migration_classification
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.migration_classification FROM PUBLIC;
GRANT SELECT ON gptbridge_index.migration_classification TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.migration_classification
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS migration_class_type_idx
    ON gptbridge_index.migration_classification (change_type);

-- ============================================================================
-- classify_migration() — classify a migration
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.classify_migration(
    p_migration_id integer,
    p_migration_name text,
    p_change_type text,
    p_classified_by text,
    p_pre_migration text DEFAULT NULL,
    p_data_transform text DEFAULT NULL,
    p_compatibility_window_days integer DEFAULT NULL,
    p_post_migration text DEFAULT NULL,
    p_rollback_plan text DEFAULT NULL,
    p_recovery_plan text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    IF p_change_type = 'breaking' THEN
        IF p_pre_migration IS NULL OR p_rollback_plan IS NULL THEN
            RAISE EXCEPTION
                'Breaking migration % requires pre_migration and rollback_plan',
                p_migration_id;
        END IF;
    END IF;

    INSERT INTO gptbridge_index.migration_classification (
        migration_id, migration_name, change_type,
        pre_migration, data_transform, compatibility_window_days,
        post_migration, rollback_plan, recovery_plan, classified_by
    )
    VALUES (
        p_migration_id, p_migration_name, p_change_type,
        p_pre_migration, p_data_transform, p_compatibility_window_days,
        p_post_migration, p_rollback_plan, p_recovery_plan, p_classified_by
    )
    ON CONFLICT (migration_id) DO UPDATE SET
        migration_name = EXCLUDED.migration_name,
        change_type = EXCLUDED.change_type,
        pre_migration = EXCLUDED.pre_migration,
        data_transform = EXCLUDED.data_transform,
        compatibility_window_days = EXCLUDED.compatibility_window_days,
        post_migration = EXCLUDED.post_migration,
        rollback_plan = EXCLUDED.rollback_plan,
        recovery_plan = EXCLUDED.recovery_plan,
        classified_by = EXCLUDED.classified_by,
        classified_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_breaking_migrations() — list all breaking migrations
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_breaking_migrations()
RETURNS TABLE (
    migration_id integer,
    migration_name text,
    rollback_plan text,
    recovery_plan text
) AS $$
BEGIN
    RETURN QUERY
    SELECT migration_id, migration_name, rollback_plan, recovery_plan
    FROM gptbridge_index.migration_classification
    WHERE change_type = 'breaking'
    ORDER BY migration_id;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
