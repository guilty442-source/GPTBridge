-- 015_retention_and_partition.sql
-- Data retention policy + partition preparation.
--
-- Retention policies (per table):
--   transport.tool_request  — retain 30 days after completion
--   audit.event             — retain 90 days
--   reconcile_conflict_log  — retain 180 days after resolution
--   inference history       — (future) retain 30 days
--
-- Partition preparation:
--   audit.event and tool_request are candidates for time-based partitioning
--   if volume grows.  For now, we add a retention function and indexes
--   that support future partitioning without breaking existing queries.
--
-- A46/E22 + A8/E21.

-- ============================================================================
-- Retention metadata table: declares the retention policy for each table.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.retention_policy (
    schema_name text NOT NULL,
    table_name text NOT NULL,
    retention_days integer NOT NULL CHECK (retention_days > 0),
    retention_column text NOT NULL DEFAULT 'created_at',
    purge_method text NOT NULL DEFAULT 'delete' CHECK (
        purge_method IN ('delete', 'archive', 'partition-drop')
    ),
    enabled boolean NOT NULL DEFAULT true,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (schema_name, table_name)
);

ALTER TABLE gptbridge_index.retention_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.retention_policy FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS retention_policy_read ON gptbridge_index.retention_policy;
CREATE POLICY retention_policy_read ON gptbridge_index.retention_policy
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS retention_policy_write ON gptbridge_index.retention_policy;
CREATE POLICY retention_policy_write ON gptbridge_index.retention_policy
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.retention_policy FROM PUBLIC;
GRANT SELECT ON gptbridge_index.retention_policy TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.retention_policy TO gptbridge_index_executor;

-- Seed default retention policies
INSERT INTO gptbridge_index.retention_policy (schema_name, table_name, retention_days, retention_column, purge_method)
VALUES
    ('gptbridge_transport', 'tool_request', 30, 'updated_at', 'delete'),
    ('gptbridge_audit', 'event', 90, 'occurred_at', 'archive'),
    ('gptbridge_index', 'reconcile_conflict_log', 180, 'detected_at', 'delete')
ON CONFLICT (schema_name, table_name) DO NOTHING;

-- ============================================================================
-- Retention purge function: safely deletes/archives expired rows.
-- Returns a summary of purged counts.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.run_retention_purge(
    p_dry_run boolean DEFAULT true
) RETURNS TABLE(
    schema_name text,
    table_name text,
    rows_purged bigint
) AS $$
DECLARE
    policy_record RECORD;
    purge_sql text;
    count_result bigint;
BEGIN
    FOR policy_record IN
        SELECT * FROM gptbridge_index.retention_policy WHERE enabled = true
    LOOP
        IF policy_record.purge_method = 'delete' THEN
            EXECUTE format(
                'SELECT count(*) FROM %I.%I WHERE %I < now() - interval ''%s days''',
                policy_record.schema_name,
                policy_record.table_name,
                policy_record.retention_column,
                policy_record.retention_days
            ) INTO count_result;
            IF NOT p_dry_run THEN
                EXECUTE format(
                    'DELETE FROM %I.%I WHERE %I < now() - interval ''%s days''',
                    policy_record.schema_name,
                    policy_record.table_name,
                    policy_record.retention_column,
                    policy_record.retention_days
                );
            END IF;
            schema_name := policy_record.schema_name;
            table_name := policy_record.table_name;
            rows_purged := count_result;
            RETURN NEXT;
        END IF;
    END LOOP;
    RETURN;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- Partition preparation: add indexes on the retention column to support
-- future range partitioning by date without query changes.
-- ============================================================================
CREATE INDEX IF NOT EXISTS audit_event_occurred_at_idx
    ON gptbridge_audit.event (occurred_at);

CREATE INDEX IF NOT EXISTS tool_request_updated_at_idx
    ON gptbridge_transport.tool_request (updated_at)
    WHERE status IN ('completed', 'cancelled', 'dead-letter');
