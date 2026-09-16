-- 035_audit_hot_history_separation.sql
-- Audit Hot/History Separation: archive old audit events to keep the
-- hot audit table small.  Sets thresholds for when to enable partitioning.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- audit_event_history — archive table for old audit events
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_audit.event_history (
    LIKE gptbridge_audit.event INCLUDING ALL
);

ALTER TABLE gptbridge_audit.event_history
    ADD COLUMN IF NOT EXISTS archived_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE gptbridge_audit.event_history ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_audit.event_history FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS event_history_read ON gptbridge_audit.event_history;
CREATE POLICY event_history_read ON gptbridge_audit.event_history
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

REVOKE ALL ON gptbridge_audit.event_history FROM PUBLIC;
GRANT SELECT ON gptbridge_audit.event_history TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_audit.event_history TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS event_history_archived_idx
    ON gptbridge_audit.event_history (archived_at);
CREATE INDEX IF NOT EXISTS event_history_occurred_idx
    ON gptbridge_audit.event_history (occurred_at);

-- ============================================================================
-- archive_audit_events() — move old audit events to history
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_audit.archive_audit_events(
    p_older_than_days integer DEFAULT 30,
    p_batch_limit integer DEFAULT 1000
) RETURNS integer AS $$
DECLARE
    v_count integer;
BEGIN
    WITH moved AS (
        DELETE FROM gptbridge_audit.event
        WHERE occurred_at < now() - (p_older_than_days || ' days')::interval
        LIMIT p_batch_limit
        RETURNING *
    )
    INSERT INTO gptbridge_audit.event_history
    SELECT *, now() FROM moved;

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER
    SET search_path = pg_catalog, gptbridge_audit, gptbridge_security;

-- ============================================================================
-- partition_threshold — when to enable partitioning (E5)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.partition_threshold (
    table_schema text NOT NULL,
    table_name text NOT NULL,
    row_count_threshold bigint NOT NULL DEFAULT 1000000,
    size_mb_threshold bigint NOT NULL DEFAULT 1024,
    latency_ms_threshold real NOT NULL DEFAULT 100,
    partition_enabled boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (table_schema, table_name)
);

ALTER TABLE gptbridge_index.partition_threshold ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.partition_threshold FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS partition_threshold_read ON gptbridge_index.partition_threshold;
CREATE POLICY partition_threshold_read ON gptbridge_index.partition_threshold
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS partition_threshold_write ON gptbridge_index.partition_threshold;
CREATE POLICY partition_threshold_write ON gptbridge_index.partition_threshold
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.partition_threshold FROM PUBLIC;
GRANT SELECT ON gptbridge_index.partition_threshold TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.partition_threshold
    TO gptbridge_index_executor;

-- Seed thresholds for hot tables
INSERT INTO gptbridge_index.partition_threshold (
    table_schema, table_name, row_count_threshold, size_mb_threshold, latency_ms_threshold
) VALUES
    ('gptbridge_transport', 'tool_request', 1000000, 1024, 100),
    ('gptbridge_audit', 'event', 5000000, 2048, 50)
ON CONFLICT DO NOTHING;
