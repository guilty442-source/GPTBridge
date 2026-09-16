-- 017_maintenance_window.sql
-- Maintenance Window Policy: VACUUM, ANALYZE, index rebuild, and large
-- migrations are separated from general runtime to avoid interference with
-- transport peak traffic.
--
-- A8/E21 + A44/E30.

CREATE TABLE IF NOT EXISTS gptbridge_index.maintenance_window (
    window_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    operation text NOT NULL CHECK (
        operation IN ('vacuum', 'analyze', 'index-rebuild', 'migration', 'reindex', 'backup')
    ),
    target_schema text,
    target_table text,
    scheduled_start timestamptz NOT NULL,
    scheduled_end timestamptz NOT NULL,
    actual_start timestamptz,
    actual_end timestamptz,
    status text NOT NULL DEFAULT 'scheduled' CHECK (
        status IN ('scheduled', 'running', 'completed', 'failed', 'cancelled')
    ),
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    created_by text NOT NULL
);

ALTER TABLE gptbridge_index.maintenance_window ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.backend_generation_state FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS maintenance_window_read ON gptbridge_index.maintenance_window;
CREATE POLICY maintenance_window_read ON gptbridge_index.maintenance_window
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS maintenance_window_write ON gptbridge_index.maintenance_window;
CREATE POLICY maintenance_window_write ON gptbridge_index.maintenance_window
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.maintenance_window FROM PUBLIC;
GRANT SELECT ON gptbridge_index.maintenance_window TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.maintenance_window TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS maintenance_window_scheduled_idx
    ON gptbridge_index.maintenance_window (scheduled_start, status);

-- Function to check if a maintenance window is currently active
CREATE OR REPLACE FUNCTION gptbridge_index.maintenance_window_active(
    p_operation text DEFAULT NULL
) RETURNS boolean AS $$
BEGIN
    IF p_operation IS NULL THEN
        RETURN EXISTS(
            SELECT 1 FROM gptbridge_index.maintenance_window
            WHERE status = 'running'
              AND scheduled_start <= now()
        );
    END IF;
    RETURN EXISTS(
        SELECT 1 FROM gptbridge_index.maintenance_window
        WHERE status = 'running'
          AND operation = p_operation
          AND scheduled_start <= now()
    );
END;
$$ LANGUAGE plpgsql STABLE;

-- Function to check if a maintenance window is scheduled soon (within 5 min)
CREATE OR REPLACE FUNCTION gptbridge_index.maintenance_window_scheduled_soon(
    p_within_minutes integer DEFAULT 5
) RETURNS boolean AS $$
BEGIN
    RETURN EXISTS(
        SELECT 1 FROM gptbridge_index.maintenance_window
        WHERE status = 'scheduled'
          AND scheduled_start <= now() + (p_within_minutes || ' minutes')::interval
          AND scheduled_start >= now()
    );
END;
$$ LANGUAGE plpgsql STABLE;
