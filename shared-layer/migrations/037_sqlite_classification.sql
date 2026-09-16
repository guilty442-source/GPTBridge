-- 037_sqlite_classification.sql
-- SQLite Classification: formally classify each SQLite database into
-- Class A (governance/high-integrity), B (module-private formal),
-- C (runtime/checkpoint), D (cache/fallback), with different
-- synchronous, backup frequency, integrity check, retention, and
-- reconcile requirements.
--
-- Codex basis:
--   A8/E21  — SQLite: owner-private-operational-state.
--   A44/E30 — four-functions-local.

-- ============================================================================
-- sqlite_database_class — registry of each SQLite database and its class
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.sqlite_database_class (
    module_id text NOT NULL,
    database_path text NOT NULL,
    db_class text NOT NULL CHECK (db_class IN ('A', 'B', 'C', 'D')),
    synchronous_setting text NOT NULL DEFAULT 'NORMAL',
    backup_frequency_seconds bigint NOT NULL DEFAULT 3600,
    integrity_check_frequency_seconds bigint NOT NULL DEFAULT 86400,
    retention_days integer NOT NULL DEFAULT 30,
    reconcile_required boolean NOT NULL DEFAULT true,
    description text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (module_id, database_path)
);

ALTER TABLE gptbridge_index.sqlite_database_class ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.sqlite_database_class FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sqlite_class_read ON gptbridge_index.sqlite_database_class;
CREATE POLICY sqlite_class_read ON gptbridge_index.sqlite_database_class
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS sqlite_class_write ON gptbridge_index.sqlite_database_class;
CREATE POLICY sqlite_class_write ON gptbridge_index.sqlite_database_class
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.sqlite_database_class FROM PUBLIC;
GRANT SELECT ON gptbridge_index.sqlite_database_class TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.sqlite_database_class
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS sqlite_class_idx
    ON gptbridge_index.sqlite_database_class (db_class);

-- ============================================================================
-- upsert_sqlite_class() — register or update a SQLite database's class
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.upsert_sqlite_class(
    p_module_id text,
    p_database_path text,
    p_db_class text,
    p_description text DEFAULT NULL
) RETURNS void AS $$
DECLARE
    v_sync text;
    v_backup bigint;
    v_integrity bigint;
    v_retention integer;
    v_reconcile boolean;
BEGIN
    -- Set policy based on class
    v_sync := CASE p_db_class
        WHEN 'A' THEN 'FULL'
        WHEN 'B' THEN 'NORMAL'
        WHEN 'C' THEN 'NORMAL'
        WHEN 'D' THEN 'NORMAL'
    END;
    v_backup := CASE p_db_class
        WHEN 'A' THEN 86400   -- 24h
        WHEN 'B' THEN 7200    -- 2h
        WHEN 'C' THEN 3600    -- 1h
        WHEN 'D' THEN 7200    -- 2h
    END;
    v_integrity := CASE p_db_class
        WHEN 'A' THEN 3600   -- 1h
        WHEN 'B' THEN 86400   -- 24h
        WHEN 'C' THEN 86400   -- 24h
        WHEN 'D' THEN 604800  -- 7 days
    END;
    v_retention := CASE p_db_class
        WHEN 'A' THEN 365
        WHEN 'B' THEN 90
        WHEN 'C' THEN 30
        WHEN 'D' THEN 7
    END;
    v_reconcile := p_db_class IN ('A', 'B', 'C');

    INSERT INTO gptbridge_index.sqlite_database_class (
        module_id, database_path, db_class,
        synchronous_setting, backup_frequency_seconds,
        integrity_check_frequency_seconds, retention_days,
        reconcile_required, description
    )
    VALUES (
        p_module_id, p_database_path, p_db_class,
        v_sync, v_backup, v_integrity, v_retention,
        v_reconcile, p_description
    )
    ON CONFLICT (module_id, database_path) DO UPDATE SET
        db_class = p_db_class,
        synchronous_setting = v_sync,
        backup_frequency_seconds = v_backup,
        integrity_check_frequency_seconds = v_integrity,
        retention_days = v_retention,
        reconcile_required = v_reconcile,
        description = p_description,
        updated_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
