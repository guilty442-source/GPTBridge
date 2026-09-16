-- 101_sqlite_single_recovery.sql
-- SQLite Single-DB Recovery.
--
-- SQLite single-DB failure:
--   open failure → integrity check → WAL recovery → backup restore if required →
--   schema validation → generation validation
-- Cache/fallback DB: rebuild preferred over complex repair.
-- Official private state: restore + verification.
--
-- Codex basis:
--   A44/E30 — four-functions-local.
--   A8/E21  — SQLite: owner-private-operational-state.

CREATE TABLE IF NOT EXISTS gptbridge_index.sqlite_single_recovery (
    recovery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    module_id text NOT NULL,
    database_path text NOT NULL,
    database_class text,  -- 'A', 'B', 'C', 'D'
    failure_type text NOT NULL CHECK (failure_type IN (
        'open_failure', 'integrity_check_failed', 'wal_corruption',
        'schema_mismatch', 'generation_mismatch', 'disk_error'
    )),
    recovery_action text NOT NULL DEFAULT 'integrity_check' CHECK (recovery_action IN (
        'integrity_check', 'wal_recovery', 'backup_restore',
        'rebuild_empty', 'schema_validation', 'generation_validation', 'completed'
    )),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    status text NOT NULL DEFAULT 'in_progress' CHECK (status IN (
        'in_progress', 'completed', 'failed', 'manual_intervention'
    )),
    restored_from_backup_id text,
    new_generation integer,
    notes text
);

ALTER TABLE gptbridge_index.sqlite_single_recovery ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.sqlite_single_recovery FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sqlite_single_rec_read ON gptbridge_index.sqlite_single_recovery;
CREATE POLICY sqlite_single_rec_read ON gptbridge_index.sqlite_single_recovery
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS sqlite_single_rec_write ON gptbridge_index.sqlite_single_recovery;
CREATE POLICY sqlite_single_rec_write ON gptbridge_index.sqlite_single_recovery
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.sqlite_single_recovery FROM PUBLIC;
GRANT SELECT ON gptbridge_index.sqlite_single_recovery TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.sqlite_single_recovery
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS sqlite_single_rec_module_idx
    ON gptbridge_index.sqlite_single_recovery (module_id, status);

CREATE OR REPLACE FUNCTION gptbridge_index.start_sqlite_recovery(
    p_incident_id uuid,
    p_module_id text,
    p_database_path text,
    p_failure_type text,
    p_database_class text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.sqlite_single_recovery (
        incident_id, module_id, database_path,
        failure_type, database_class
    )
    VALUES (p_incident_id, p_module_id, p_database_path,
            p_failure_type, p_database_class)
    RETURNING recovery_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.complete_sqlite_recovery(
    p_recovery_id uuid,
    p_status text,
    p_restored_from_backup_id text DEFAULT NULL,
    p_new_generation integer DEFAULT NULL,
    p_notes text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.sqlite_single_recovery
    SET status = p_status,
        recovery_action = 'completed',
        completed_at = now(),
        restored_from_backup_id = p_restored_from_backup_id,
        new_generation = p_new_generation,
        notes = p_notes
    WHERE recovery_id = p_recovery_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
