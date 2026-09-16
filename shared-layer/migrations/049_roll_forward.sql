-- 049_roll_forward.sql
-- Roll Forward Policy.
--
-- Production databases prefer roll-forward over destructive rollback,
-- because schema rollback on data that already has new-schema rows
-- can be more dangerous than a corrective migration.
--
-- When migration N has a problem:
--   Preferred: N+1 corrective migration
--   Not: reverse the already-applied migration
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- roll_forward_migration — tracks corrective migrations
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.roll_forward_migration (
    corrective_migration_id integer PRIMARY KEY,
    fixes_migration_id integer NOT NULL,  -- the migration that had the problem
    fixes_release_id text REFERENCES gptbridge_index.database_release(release_id),
    description text NOT NULL,
    corrective_sql text NOT NULL,
    verification_sql text,
    applied_at timestamptz NOT NULL DEFAULT now(),
    applied_by text NOT NULL,
    verified boolean NOT NULL DEFAULT false,
    verified_at timestamptz
);

ALTER TABLE gptbridge_index.roll_forward_migration ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.roll_forward_migration FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS roll_forward_read ON gptbridge_index.roll_forward_migration;
CREATE POLICY roll_forward_read ON gptbridge_index.roll_forward_migration
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS roll_forward_write ON gptbridge_index.roll_forward_migration;
CREATE POLICY roll_forward_write ON gptbridge_index.roll_forward_migration
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.roll_forward_migration FROM PUBLIC;
GRANT SELECT ON gptbridge_index.roll_forward_migration TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.roll_forward_migration
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS roll_forward_fixes_idx
    ON gptbridge_index.roll_forward_migration (fixes_migration_id);

-- ============================================================================
-- record_roll_forward() — record a corrective migration
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_roll_forward(
    p_corrective_migration_id integer,
    p_fixes_migration_id integer,
    p_description text,
    p_corrective_sql text,
    p_applied_by text,
    p_fixes_release_id text DEFAULT NULL,
    p_verification_sql text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    INSERT INTO gptbridge_index.roll_forward_migration (
        corrective_migration_id, fixes_migration_id, fixes_release_id,
        description, corrective_sql, verification_sql, applied_by
    )
    VALUES (
        p_corrective_migration_id, p_fixes_migration_id, p_fixes_release_id,
        p_description, p_corrective_sql, p_verification_sql, p_applied_by
    )
    ON CONFLICT (corrective_migration_id) DO UPDATE SET
        description = EXCLUDED.description,
        corrective_sql = EXCLUDED.corrective_sql,
        verification_sql = EXCLUDED.verification_sql,
        applied_by = EXCLUDED.applied_by,
        applied_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- verify_roll_forward() — mark a corrective migration as verified
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.verify_roll_forward(
    p_corrective_migration_id integer
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.roll_forward_migration
    SET verified = true,
        verified_at = now()
    WHERE corrective_migration_id = p_corrective_migration_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_roll_forwards_for() — get corrective migrations for a given migration
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_roll_forwards_for(
    p_migration_id integer
) RETURNS TABLE (
    corrective_migration_id integer,
    description text,
    applied_at timestamptz,
    verified boolean
) AS $$
BEGIN
    RETURN QUERY
    SELECT corrective_migration_id, description, applied_at, verified
    FROM gptbridge_index.roll_forward_migration
    WHERE fixes_migration_id = p_migration_id
    ORDER BY corrective_migration_id;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
