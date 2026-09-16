-- 062_capacity_quota.sql
-- Capacity Quota.
--
-- Per data domain:
--   soft_limit, hard_limit, archive_threshold, emergency_threshold
--
-- Domains: transport history, audit, repair history, inference records,
-- training logs
--
-- Prevents one category of historical data from filling the disk.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- capacity_quota — per-domain capacity limits
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.capacity_quota (
    domain_name text PRIMARY KEY,  -- 'transport_history', 'audit', etc.
    source_schema text,
    source_table text,
    soft_limit_mb integer NOT NULL,      -- warn
    hard_limit_mb integer NOT NULL,      -- reject new writes
    archive_threshold_mb integer NOT NULL,  -- trigger archiving
    emergency_threshold_mb integer NOT NULL, -- trigger emergency purge
    current_size_mb numeric DEFAULT 0,
    current_row_count bigint DEFAULT 0,
    last_measured_at timestamptz,
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.capacity_quota ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.capacity_quota FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS capacity_quota_read ON gptbridge_index.capacity_quota;
CREATE POLICY capacity_quota_read ON gptbridge_index.capacity_quota
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS capacity_quota_write ON gptbridge_index.capacity_quota;
CREATE POLICY capacity_quota_write ON gptbridge_index.capacity_quota
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.capacity_quota FROM PUBLIC;
GRANT SELECT ON gptbridge_index.capacity_quota TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.capacity_quota
    TO gptbridge_index_executor;

-- Seed default quotas
INSERT INTO gptbridge_index.capacity_quota
    (domain_name, source_schema, source_table,
     soft_limit_mb, hard_limit_mb, archive_threshold_mb, emergency_threshold_mb,
     description)
VALUES
    ('transport_history', 'gptbridge_transport', 'tool_request_history',
     500, 1000, 800, 950, 'transport request history'),
    ('audit', 'gptbridge_audit', 'event',
     1000, 2000, 1500, 1900, 'central audit ledger'),
    ('audit_archive', 'gptbridge_audit', 'event_history',
     2000, 4000, 3000, 3800, 'archived audit events'),
    ('repair_history', NULL, NULL,
     200, 500, 400, 480, 'repair history records'),
    ('inference_records', NULL, NULL,
     500, 1000, 800, 950, 'model inference records'),
    ('training_logs', NULL, NULL,
     1000, 2000, 1500, 1900, 'training logs')
ON CONFLICT (domain_name) DO NOTHING;

-- ============================================================================
-- update_capacity_measurement() — update current size for a domain
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.update_capacity_measurement(
    p_domain_name text,
    p_size_mb numeric,
    p_row_count bigint
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.capacity_quota
    SET current_size_mb = p_size_mb,
        current_row_count = p_row_count,
        last_measured_at = now(),
        updated_at = now()
    WHERE domain_name = p_domain_name;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- check_capacity_status() — check if a domain is over any threshold
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.check_capacity_status(
    p_domain_name text
) RETURNS TABLE (
    status text,  -- 'ok', 'soft', 'archive', 'hard', 'emergency'
    current_mb numeric,
    threshold_mb integer
) AS $$
DECLARE
    v_quota record;
BEGIN
    SELECT * INTO v_quota
    FROM gptbridge_index.capacity_quota
    WHERE domain_name = p_domain_name;

    IF v_quota IS NULL THEN
        RETURN QUERY SELECT 'unknown'::text, 0::numeric, 0::integer;
        RETURN;
    END IF;

    IF v_quota.current_size_mb >= v_quota.emergency_threshold_mb THEN
        RETURN QUERY SELECT 'emergency'::text, v_quota.current_size_mb,
                            v_quota.emergency_threshold_mb;
    ELSIF v_quota.current_size_mb >= v_quota.hard_limit_mb THEN
        RETURN QUERY SELECT 'hard'::text, v_quota.current_size_mb,
                            v_quota.hard_limit_mb;
    ELSIF v_quota.current_size_mb >= v_quota.archive_threshold_mb THEN
        RETURN QUERY SELECT 'archive'::text, v_quota.current_size_mb,
                            v_quota.archive_threshold_mb;
    ELSIF v_quota.current_size_mb >= v_quota.soft_limit_mb THEN
        RETURN QUERY SELECT 'soft'::text, v_quota.current_size_mb,
                            v_quota.soft_limit_mb;
    ELSE
        RETURN QUERY SELECT 'ok'::text, v_quota.current_size_mb, 0::integer;
    END IF;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
