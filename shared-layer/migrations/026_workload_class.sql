-- 026_workload_class.sql
-- Workload Class: classify SQL into six classes with different timeout,
-- pool, and priority, so they don't compete for resources.
--
-- Classes:
--   interactive     — user-facing queries (short timeout, high priority)
--   transport       — cross-tool transport (medium timeout)
--   audit           — audit logging (medium timeout)
--   reconciliation  — SQLite→PG reconcile (long timeout)
--   maintenance     — VACUUM/ANALYZE/index rebuild (long timeout, low priority)
--   migration       — schema migration (very long timeout, exclusive)
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A44/E30 — four-functions-local.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- workload_class table — declares timeout/pool/priority per class.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.workload_class (
    class_name text PRIMARY KEY,
    pool_owner text NOT NULL,
    statement_timeout_ms integer NOT NULL,
    lock_timeout_ms integer NOT NULL,
    priority text NOT NULL DEFAULT 'normal' CHECK (
        priority IN ('high', 'normal', 'low', 'exclusive')
    ),
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.workload_class ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.workload_class FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS workload_class_read ON gptbridge_index.workload_class;
CREATE POLICY workload_class_read ON gptbridge_index.workload_class
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS workload_class_write ON gptbridge_index.workload_class;
CREATE POLICY workload_class_write ON gptbridge_index.workload_class
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.workload_class FROM PUBLIC;
GRANT SELECT ON gptbridge_index.workload_class TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.workload_class
    TO gptbridge_index_executor;

-- Seed the six workload classes
INSERT INTO gptbridge_index.workload_class (
    class_name, pool_owner, statement_timeout_ms, lock_timeout_ms, priority, description
) VALUES
    ('interactive',    'central-index',  5000,  2000, 'high',      'User-facing queries'),
    ('transport',      'transport',     30000,  5000, 'normal',    'Cross-tool transport'),
    ('audit',          'audit',         10000,  2000, 'normal',    'Audit logging'),
    ('reconciliation', 'central-index', 120000, 10000, 'low',       'SQLite→PG reconciliation'),
    ('maintenance',    'central-index', 300000, 30000, 'low',       'VACUUM/ANALYZE/index rebuild'),
    ('migration',      'central-index', 600000, 60000, 'exclusive', 'Schema migration')
ON CONFLICT (class_name) DO NOTHING;

-- ============================================================================
-- apply_workload_class() — sets statement_timeout and lock_timeout based
-- on the session's declared workload class (gptbridge.workload_class).
-- Called by a trigger before writes, or the runtime can call it directly.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.apply_workload_class()
RETURNS void AS $$
DECLARE
    v_class text;
    v_stmt_timeout integer;
    v_lock_timeout integer;
BEGIN
    v_class := nullif(current_setting('gptbridge.workload_class', true), '');
    IF v_class IS NULL THEN
        RETURN; -- no class declared, use defaults
    END IF;

    SELECT statement_timeout_ms, lock_timeout_ms
    INTO v_stmt_timeout, v_lock_timeout
    FROM gptbridge_index.workload_class
    WHERE class_name = v_class;

    IF v_stmt_timeout IS NOT NULL THEN
        PERFORM set_config('statement_timeout', v_stmt_timeout::text || 'ms', true);
        PERFORM set_config('lock_timeout', v_lock_timeout::text || 'ms', true);
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
