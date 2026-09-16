-- 029_watchdog_bloat_rpo_rto.sql
-- Long Transaction Watchdog + Bloat/Vacuum tracking + RPO/RTO classes +
-- Capacity thresholds.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A44/E30 — four-functions-local.
--   A46/E22 — Audit: mandatory-ledger.
--
-- This migration adds:
--   * gptbridge_index.long_transaction_watchdog — records long-running
--     transactions and idle-in-transaction sessions that exceed thresholds.
--   * gptbridge_index.bloat_report — periodic table/index bloat, dead
--     tuple, and autovacuum lag snapshots.
--   * gptbridge_index.rpo_rto_class — per-engine RPO/RTO classes.
--   * gptbridge_index.capacity_threshold — warning/critical/fail-closed
--     thresholds for disk, WAL, SQLite WAL, transport backlog, Qdrant size.

-- ============================================================================
-- long_transaction_watchdog
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.long_transaction_watchdog (
    watchdog_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    pid integer NOT NULL,
    session_user text NOT NULL,
    state text NOT NULL,
    query text,
    transaction_age_seconds bigint NOT NULL,
    idle_in_transaction_seconds bigint,
    lock_holder boolean NOT NULL DEFAULT false,
    threshold_seconds bigint NOT NULL,
    action_taken text,
    detected_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.long_transaction_watchdog ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.long_transaction_watchdog FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS watchdog_read ON gptbridge_index.long_transaction_watchdog;
CREATE POLICY watchdog_read ON gptbridge_index.long_transaction_watchdog
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS watchdog_write ON gptbridge_index.long_transaction_watchdog;
CREATE POLICY watchdog_write ON gptbridge_index.long_transaction_watchdog
    FOR INSERT WITH CHECK (true);

REVOKE ALL ON gptbridge_index.long_transaction_watchdog FROM PUBLIC;
GRANT SELECT ON gptbridge_index.long_transaction_watchdog TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_index.long_transaction_watchdog TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS watchdog_detected_idx
    ON gptbridge_index.long_transaction_watchdog (detected_at);
CREATE INDEX IF NOT EXISTS watchdog_pid_idx
    ON gptbridge_index.long_transaction_watchdog (pid, detected_at);

-- ============================================================================
-- bloat_report — periodic bloat/dead-tuple/autovacuum snapshots
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.bloat_report (
    bloat_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    schema_name text NOT NULL,
    table_name text NOT NULL,
    estimated_bloat_percent real NOT NULL DEFAULT 0,
    dead_tuples bigint NOT NULL DEFAULT 0,
    live_tuples bigint NOT NULL DEFAULT 0,
    last_autovacuum timestamptz,
    autovacuum_count bigint NOT NULL DEFAULT 0,
    last_analyze timestamptz,
    table_size_bytes bigint NOT NULL DEFAULT 0,
    index_size_bytes bigint NOT NULL DEFAULT 0,
    collected_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.bloat_report ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.bloat_report FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS bloat_read ON gptbridge_index.bloat_report;
CREATE POLICY bloat_read ON gptbridge_index.bloat_report
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS bloat_write ON gptbridge_index.bloat_report;
CREATE POLICY bloat_write ON gptbridge_index.bloat_report
    FOR INSERT WITH CHECK (true);

REVOKE ALL ON gptbridge_index.bloat_report FROM PUBLIC;
GRANT SELECT ON gptbridge_index.bloat_report TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_index.bloat_report TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS bloat_collected_idx
    ON gptbridge_index.bloat_report (collected_at);
CREATE INDEX IF NOT EXISTS bloat_table_idx
    ON gptbridge_index.bloat_report (schema_name, table_name, collected_at);

-- ============================================================================
-- rpo_rto_class — per-engine RPO/RTO classes
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.rpo_rto_class (
    engine text PRIMARY KEY CHECK (engine IN (
        'postgresql-central', 'governance-codex-sqlite',
        'module-sqlite', 'qdrant'
    )),
    rpo_seconds bigint NOT NULL,  -- max acceptable data loss window
    rto_seconds bigint NOT NULL,  -- max acceptable downtime
    backup_frequency_seconds bigint NOT NULL,
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.rpo_rto_class ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.rpo_rto_class FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS rpo_rto_read ON gptbridge_index.rpo_rto_class;
CREATE POLICY rpo_rto_read ON gptbridge_index.rpo_rto_class
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS rpo_rto_write ON gptbridge_index.rpo_rto_class;
CREATE POLICY rpo_rto_write ON gptbridge_index.rpo_rto_class
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.rpo_rto_class FROM PUBLIC;
GRANT SELECT ON gptbridge_index.rpo_rto_class TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.rpo_rto_class
    TO gptbridge_index_executor;

-- Seed default RPO/RTO classes
INSERT INTO gptbridge_index.rpo_rto_class (
    engine, rpo_seconds, rto_seconds, backup_frequency_seconds, description
) VALUES
    ('postgresql-central',       300,    600,  3600, 'Central PostgreSQL: 5min RPO, 10min RTO'),
    ('governance-codex-sqlite',  86400,  3600, 86400, 'Governance codex SQLite: 24h RPO, 1h RTO'),
    ('module-sqlite',            3600,  1800,  7200, 'Module SQLite: 1h RPO, 30min RTO'),
    ('qdrant',                   3600,  1800,  7200, 'Qdrant: 1h RPO, 30min RTO')
ON CONFLICT (engine) DO NOTHING;

-- ============================================================================
-- capacity_threshold — warning/critical/fail-closed thresholds
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.capacity_threshold (
    metric_name text PRIMARY KEY,
    warning_level real NOT NULL,
    critical_level real NOT NULL,
    fail_closed_level real NOT NULL,
    unit text NOT NULL,
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.capacity_threshold ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.capacity_threshold FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS capacity_read ON gptbridge_index.capacity_threshold;
CREATE POLICY capacity_read ON gptbridge_index.capacity_threshold
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS capacity_write ON gptbridge_index.capacity_threshold;
CREATE POLICY capacity_write ON gptbridge_index.capacity_threshold
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.capacity_threshold FROM PUBLIC;
GRANT SELECT ON gptbridge_index.capacity_threshold TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.capacity_threshold
    TO gptbridge_index_executor;

-- Seed default thresholds
INSERT INTO gptbridge_index.capacity_threshold (
    metric_name, warning_level, critical_level, fail_closed_level, unit, description
) VALUES
    ('disk-usage-percent',           75,  85,  95, 'percent', 'Disk usage'),
    ('wal-size-mb',                 1024, 2048, 4096, 'MB', 'PostgreSQL WAL size'),
    ('sqlite-wal-size-mb',             50,  100,  200, 'MB', 'SQLite WAL size'),
    ('transport-backlog-count',       100,  500, 1000, 'count', 'Transport pending queue'),
    ('qdrant-collection-size-mb',    2048, 4096, 8192, 'MB', 'Qdrant collection size')
ON CONFLICT (metric_name) DO NOTHING;
