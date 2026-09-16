-- 053_sqlite_per_class_retention.sql
-- SQLite Per-Class Retention.
--
-- Not all SQLite uses one retention policy:
--   governance codex   → permanent
--   identity directory  → permanent + version history
--   inference records   → time-bounded
--   runtime checkpoint  → rotatable
--   repair history      → long summary, old details can archive
--   cache               → short if rebuildable
--
-- Codex basis:
--   A8/E21  — SQLite: owner-private-operational-state.
--   A44/E30 — four-functions-local.

-- ============================================================================
-- sqlite_retention_policy — per-class retention rules
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.sqlite_retention_policy (
    db_class text PRIMARY KEY REFERENCES gptbridge_index.sqlite_database_class(db_class)
        DEFERRABLE INITIALLY DEFERRED,
    retention_days integer,  -- NULL means permanent
    archive_eligible boolean NOT NULL DEFAULT false,
    archive_after_days integer,
    purge_eligible boolean NOT NULL DEFAULT false,
    purge_after_days integer,
    version_history_required boolean NOT NULL DEFAULT false,
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.sqlite_retention_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.sqlite_retention_policy FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sqlite_retention_read ON gptbridge_index.sqlite_retention_policy;
CREATE POLICY sqlite_retention_read ON gptbridge_index.sqlite_retention_policy
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS sqlite_retention_write ON gptbridge_index.sqlite_retention_policy;
CREATE POLICY sqlite_retention_write ON gptbridge_index.sqlite_retention_policy
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.sqlite_retention_policy FROM PUBLIC;
GRANT SELECT ON gptbridge_index.sqlite_retention_policy TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.sqlite_retention_policy
    TO gptbridge_index_executor;

-- Seed defaults per class
INSERT INTO gptbridge_index.sqlite_retention_policy
    (db_class, retention_days, archive_eligible, archive_after_days,
     purge_eligible, purge_after_days, version_history_required, description)
VALUES
    ('A', NULL, false, NULL, false, NULL, true,
     'Class A — governance codex, permanent, version history required'),
    ('B', NULL, true, 90, false, NULL, true,
     'Class B — module-private formal state, permanent but old details archive after 90d'),
    ('C', 30, true, 30, true, 90, false,
     'Class C — runtime/checkpoint, 30d retention, archive then purge after 90d'),
    ('D', 7, true, 7, true, 30, false,
     'Class D — cache/fallback, 7d retention, purge after 30d')
ON CONFLICT (db_class) DO NOTHING;

-- ============================================================================
-- get_sqlite_retention_for_class() — get retention policy for a class
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_sqlite_retention_for_class(
    p_db_class text
) RETURNS TABLE (
    retention_days integer,
    archive_eligible boolean,
    archive_after_days integer,
    purge_eligible boolean,
    purge_after_days integer,
    version_history_required boolean
) AS $$
BEGIN
    RETURN QUERY
    SELECT retention_days, archive_eligible, archive_after_days,
           purge_eligible, purge_after_days, version_history_required
    FROM gptbridge_index.sqlite_retention_policy
    WHERE db_class = p_db_class;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
