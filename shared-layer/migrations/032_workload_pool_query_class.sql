-- 032_workload_pool_query_class.sql
-- Workload Pool Configuration + Query Class Formalization.
--
-- Extends migration 026 (workload_class) with:
--   * Pool-level configuration (max_connections, max_open, max_idle)
--   * Query-class-level configuration (retry_limit, batch_size, priority)
--   * Pool separation: index/transport/audit/reconcile/maintenance
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A44/E30 — four-functions-local.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- workload_pool_config — per-pool connection budget
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.workload_pool_config (
    pool_name text PRIMARY KEY,
    max_connections integer NOT NULL DEFAULT 8,
    max_open integer NOT NULL DEFAULT 6,
    max_idle integer NOT NULL DEFAULT 2,
    wait_timeout_seconds real NOT NULL DEFAULT 30.0,
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.workload_pool_config ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.workload_pool_config FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS workload_pool_read ON gptbridge_index.workload_pool_config;
CREATE POLICY workload_pool_read ON gptbridge_index.workload_pool_config
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS workload_pool_write ON gptbridge_index.workload_pool_config;
CREATE POLICY workload_pool_write ON gptbridge_index.workload_pool_config
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.workload_pool_config FROM PUBLIC;
GRANT SELECT ON gptbridge_index.workload_pool_config TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.workload_pool_config
    TO gptbridge_index_executor;

-- Seed default pool budgets (E1: prevent reconcile from starving transport)
INSERT INTO gptbridge_index.workload_pool_config (
    pool_name, max_connections, max_open, max_idle, wait_timeout_seconds, description
) VALUES
    ('index',       4, 3, 1, 15.0, 'Central index read/write pool'),
    ('transport',    4, 3, 1, 20.0, 'Cross-tool transport pool'),
    ('audit',        2, 2, 1, 10.0, 'Audit logging pool'),
    ('reconcile',    2, 2, 1, 60.0, 'SQLite→PG reconciliation pool'),
    ('maintenance',  1, 1, 0, 120.0, 'VACUUM/ANALYZE/index rebuild pool')
ON CONFLICT (pool_name) DO NOTHING;

-- ============================================================================
-- query_class — formal query class with retry/batch/priority
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.query_class (
    class_name text PRIMARY KEY,
    pool_name text NOT NULL REFERENCES gptbridge_index.workload_pool_config(pool_name),
    statement_timeout_ms integer NOT NULL,
    lock_timeout_ms integer NOT NULL,
    retry_limit integer NOT NULL DEFAULT 0,
    batch_size integer NOT NULL DEFAULT 1,
    priority text NOT NULL DEFAULT 'normal' CHECK (
        priority IN ('high', 'normal', 'low', 'exclusive')
    ),
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.query_class ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.query_class FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS query_class_read ON gptbridge_index.query_class;
CREATE POLICY query_class_read ON gptbridge_index.query_class
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS query_class_write ON gptbridge_index.query_class;
CREATE POLICY query_class_write ON gptbridge_index.query_class
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.query_class FROM PUBLIC;
GRANT SELECT ON gptbridge_index.query_class TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.query_class
    TO gptbridge_index_executor;

-- Seed the seven formal query classes (E2)
INSERT INTO gptbridge_index.query_class (
    class_name, pool_name, statement_timeout_ms, lock_timeout_ms,
    retry_limit, batch_size, priority, description
) VALUES
    ('interactive',    'index',       3000,   1000, 0,   1, 'high',      'User-facing queries (1-3s)'),
    ('transport',      'transport',   5000,   2000, 3,  10, 'normal',    'Cross-tool transport (3-5s)'),
    ('index_lookup',   'index',       2000,   1000, 0,   1, 'high',      'Resource/locator lookups'),
    ('audit_write',    'audit',      10000,   2000, 1, 100, 'normal',    'Audit logging'),
    ('reconcile',      'reconcile',  60000,  10000, 2, 500, 'low',       'SQLite→PG reconciliation (30-60s)'),
    ('migration',      'maintenance', 600000, 60000, 0, 1, 'exclusive', 'Schema migration (maintenance window)'),
    ('maintenance',    'maintenance', 300000, 30000, 0, 1, 'low',       'VACUUM/ANALYZE/index rebuild')
ON CONFLICT (class_name) DO NOTHING;

-- ============================================================================
-- apply_query_class() — sets statement_timeout/lock_timeout based on
-- the session's declared query class (gptbridge.query_class).
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.apply_query_class()
RETURNS void AS $$
DECLARE
    v_class text;
    v_stmt_timeout integer;
    v_lock_timeout integer;
BEGIN
    v_class := nullif(current_setting('gptbridge.query_class', true), '');
    IF v_class IS NULL THEN
        RETURN;
    END IF;

    SELECT statement_timeout_ms, lock_timeout_ms
    INTO v_stmt_timeout, v_lock_timeout
    FROM gptbridge_index.query_class
    WHERE class_name = v_class;

    IF v_stmt_timeout IS NOT NULL THEN
        PERFORM set_config('statement_timeout', v_stmt_timeout::text || 'ms', true);
        PERFORM set_config('lock_timeout', v_lock_timeout::text || 'ms', true);
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
