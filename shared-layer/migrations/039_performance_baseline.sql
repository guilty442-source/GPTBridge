-- 039_performance_baseline.sql
-- Performance Baseline: record baseline metrics so future changes can
-- answer "did this make things faster or slower?"
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- performance_baseline — baseline metrics for key operations
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.performance_baseline (
    baseline_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    operation_name text NOT NULL,
    p50_latency_ms real NOT NULL,
    p95_latency_ms real NOT NULL,
    p99_latency_ms real NOT NULL,
    sample_count integer NOT NULL,
    baseline_at timestamptz NOT NULL DEFAULT now(),
    description text,
    UNIQUE (operation_name, baseline_at)
);

ALTER TABLE gptbridge_index.performance_baseline ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.performance_baseline FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS perf_baseline_read ON gptbridge_index.performance_baseline;
CREATE POLICY perf_baseline_read ON gptbridge_index.performance_baseline
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS perf_baseline_write ON gptbridge_index.performance_baseline;
CREATE POLICY perf_baseline_write ON gptbridge_index.performance_baseline
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.performance_baseline FROM PUBLIC;
GRANT SELECT ON gptbridge_index.performance_baseline TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.performance_baseline
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS perf_baseline_op_idx
    ON gptbridge_index.performance_baseline (operation_name, baseline_at);

-- ============================================================================
-- record_baseline() — record a baseline measurement
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_baseline(
    p_operation_name text,
    p_p50_ms real,
    p_p95_ms real,
    p_p99_ms real,
    p_sample_count integer,
    p_description text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.performance_baseline (
        operation_name, p50_latency_ms, p95_latency_ms, p99_latency_ms,
        sample_count, description
    )
    VALUES (
        p_operation_name, p_p50_ms, p_p95_ms, p_p99_ms,
        p_sample_count, p_description
    )
    RETURNING baseline_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_latest_baseline() — get the most recent baseline for an operation
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_latest_baseline(
    p_operation_name text
) RETURNS TABLE (
    p50_latency_ms real,
    p95_latency_ms real,
    p99_latency_ms real,
    sample_count integer,
    baseline_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT p50_latency_ms, p95_latency_ms, p99_latency_ms,
           sample_count, baseline_at
    FROM gptbridge_index.performance_baseline
    WHERE operation_name = p_operation_name
    ORDER BY baseline_at DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- compare_baseline() — compare two baselines for an operation
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.compare_baseline(
    p_operation_name text,
    p_before_at timestamptz DEFAULT NULL,
    p_after_at timestamptz DEFAULT NULL
) RETURNS TABLE (
    before_p50_ms real,
    after_p50_ms real,
    p50_delta_pct real,
    before_p95_ms real,
    after_p95_ms real,
    p95_delta_pct real
) AS $$
DECLARE
    v_before record;
    v_after record;
BEGIN
    SELECT * INTO v_before
    FROM gptbridge_index.performance_baseline
    WHERE operation_name = p_operation_name
      AND (p_before_at IS NULL OR baseline_at <= p_before_at)
    ORDER BY baseline_at DESC LIMIT 1;

    SELECT * INTO v_after
    FROM gptbridge_index.performance_baseline
    WHERE operation_name = p_operation_name
      AND (p_after_at IS NULL OR baseline_at >= p_after_at)
      AND (p_before_at IS NULL OR baseline_at > p_before_at)
    ORDER BY baseline_at DESC LIMIT 1;

    IF v_before IS NULL OR v_after IS NULL THEN
        RETURN;
    END IF;

    RETURN QUERY SELECT
        v_before.p50_latency_ms,
        v_after.p50_latency_ms,
        CASE WHEN v_before.p50_latency_ms > 0
             THEN ((v_after.p50_latency_ms - v_before.p50_latency_ms) /
                   v_before.p50_latency_ms * 100)
             ELSE 0 END,
        v_before.p95_latency_ms,
        v_after.p95_latency_ms,
        CASE WHEN v_before.p95_latency_ms > 0
             THEN ((v_after.p95_latency_ms - v_before.p95_latency_ms) /
                   v_before.p95_latency_ms * 100)
             ELSE 0 END;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
