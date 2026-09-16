-- 031_slo_metrics.sql
-- SLO Metrics: formally define SQL layer service level objectives.
--
-- Metrics:
--   central-query-p95       — central query p95 latency (ms)
--   transport-claim-latency  — transport claim latency (ms)
--   reconcile-backlog        — reconcile backlog count
--   sqlite-lock-rate         — SQLite lock contention rate (percent)
--   qdrant-stale-rate        — Qdrant stale point rate (percent)
--   restore-success          — restore success rate (percent)
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- slo_metric — defines each SLO and its target
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.slo_metric (
    metric_name text PRIMARY KEY,
    target_value real NOT NULL,
    target_direction text NOT NULL CHECK (target_direction IN ('lower', 'higher')),
    unit text NOT NULL,
    window_seconds bigint NOT NULL DEFAULT 300,
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.slo_metric ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.slo_metric FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS slo_metric_read ON gptbridge_index.slo_metric;
CREATE POLICY slo_metric_read ON gptbridge_index.slo_metric
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS slo_metric_write ON gptbridge_index.slo_metric;
CREATE POLICY slo_metric_write ON gptbridge_index.slo_metric
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.slo_metric FROM PUBLIC;
GRANT SELECT ON gptbridge_index.slo_metric TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.slo_metric
    TO gptbridge_index_executor;

-- Seed default SLO metrics
INSERT INTO gptbridge_index.slo_metric (
    metric_name, target_value, target_direction, unit, window_seconds, description
) VALUES
    ('central-query-p95',       100,  'lower',  'ms',      300, 'Central query p95 latency'),
    ('transport-claim-latency',  500,  'lower',  'ms',      300, 'Transport claim latency'),
    ('reconcile-backlog',       1000, 'lower',  'count',   300, 'Reconcile backlog count'),
    ('sqlite-lock-rate',          5,  'lower',  'percent', 300, 'SQLite lock contention rate'),
    ('qdrant-stale-rate',         2,  'lower',  'percent', 300, 'Qdrant stale point rate'),
    ('restore-success',          99,  'higher', 'percent', 86400, 'Restore success rate')
ON CONFLICT (metric_name) DO NOTHING;

-- ============================================================================
-- slo_observation — periodic SLO measurements
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.slo_observation (
    observation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    metric_name text NOT NULL REFERENCES gptbridge_index.slo_metric(metric_name),
    observed_value real NOT NULL,
    met_target boolean NOT NULL DEFAULT false,
    observed_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.slo_observation ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.slo_observation FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS slo_obs_read ON gptbridge_index.slo_observation;
CREATE POLICY slo_obs_read ON gptbridge_index.slo_observation
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS slo_obs_write ON gptbridge_index.slo_observation;
CREATE POLICY slo_obs_write ON gptbridge_index.slo_observation
    FOR INSERT WITH CHECK (true);

REVOKE ALL ON gptbridge_index.slo_observation FROM PUBLIC;
GRANT SELECT ON gptbridge_index.slo_observation TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_index.slo_observation TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS slo_obs_metric_idx
    ON gptbridge_index.slo_observation (metric_name, observed_at);
CREATE INDEX IF NOT EXISTS slo_obs_at_idx
    ON gptbridge_index.slo_observation (observed_at);

-- ============================================================================
-- record_slo_observation() — record a SLO measurement
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_slo_observation(
    p_metric_name text,
    p_value real
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_target real;
    v_direction text;
    v_met boolean;
BEGIN
    SELECT target_value, target_direction INTO v_target, v_direction
    FROM gptbridge_index.slo_metric
    WHERE metric_name = p_metric_name;

    IF v_target IS NULL THEN
        RAISE EXCEPTION 'Unknown SLO metric: %', p_metric_name;
    END IF;

    v_met := CASE WHEN v_direction = 'lower' THEN p_value <= v_target
                  ELSE p_value >= v_target END;

    INSERT INTO gptbridge_index.slo_observation (metric_name, observed_value, met_target)
    VALUES (p_metric_name, p_value, v_met)
    RETURNING observation_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
