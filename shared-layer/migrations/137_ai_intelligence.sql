-- 137_ai_intelligence.sql
-- 星澄 AI 投資決策中心 — analysis/recommendation provenance schema.
--
-- Every analysis record traces to data sources, model version and
-- strategy version. Recommendations are append-only with immutable
-- version history; outcomes keep ai/simulated/real strictly separate.
-- trade_proposal already exists (134). No duplicate authoritative
-- account/position/market tables.

CREATE SCHEMA IF NOT EXISTS gptbridge_trading;

-- ============================================================================
-- analysis_run — one pipeline execution
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.analysis_run (
    run_id           text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    task_kind        text NOT NULL,    -- quick_market|deep_research|earnings|fund|portfolio_risk|report
    instrument_id    text,
    market           text,
    account_id       text,
    model_id         text,
    model_version    text,
    strategy_id      text,
    strategy_version text,
    data_window      text,
    stages_completed jsonb,
    stages_skipped   jsonb,
    findings         jsonb,
    missing_data     jsonb,
    degraded         boolean NOT NULL DEFAULT false,
    started_at       timestamptz NOT NULL,
    finished_at      timestamptz,
    PRIMARY KEY (run_id)
);
CREATE INDEX IF NOT EXISTS analysis_run_idx
    ON gptbridge_trading.analysis_run (task_kind, instrument_id, started_at DESC);

-- ============================================================================
-- analysis_evidence — sourced facts/results attached to runs
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.analysis_evidence (
    evidence_id     text NOT NULL,
    module_id       text NOT NULL DEFAULT 'ai-assistant',
    run_id          text REFERENCES gptbridge_trading.analysis_run(run_id),
    kind            text NOT NULL,     -- VERIFIED_FACT|CALCULATED_RESULT|MODEL_INTERPRETATION|UNVERIFIED_INFORMATION
    claim           text NOT NULL,
    value           jsonb,
    source_id       text,
    data_timestamp  timestamptz,
    data_version    text,
    computation     text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (evidence_id)
);
CREATE INDEX IF NOT EXISTS analysis_evidence_run_idx
    ON gptbridge_trading.analysis_evidence (run_id);

-- ============================================================================
-- investment_recommendation — advisory output (never executable)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.investment_recommendation (
    recommendation_id   text NOT NULL,
    module_id           text NOT NULL DEFAULT 'ai-assistant',
    account_id          text NOT NULL,
    instrument_id       text NOT NULL,
    instrument_type     text NOT NULL,   -- stock|etf|fund|index
    market              text NOT NULL,
    recommendation_type text NOT NULL,   -- BUY|SELL|HOLD|ADD|REDUCE|EXIT|REBALANCE|SUBSCRIBE|REDEEM|SWITCH
    reasoning           text,
    risk_factors        jsonb,
    evidence_refs       jsonb,
    reference_price     numeric,
    reference_nav       numeric,
    suggested_weight    numeric,
    observation_window  text,
    trigger_conditions  jsonb,
    reevaluate_conditions jsonb,
    analysis_timestamp  timestamptz NOT NULL,
    market_data_timestamp timestamptz,
    model_id            text NOT NULL,
    model_version       text,
    strategy_id         text,
    strategy_version    text,
    data_quality        text NOT NULL DEFAULT 'complete',
    status              text NOT NULL DEFAULT 'CREATED',
    expires_at          timestamptz,
    created_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (recommendation_id)
);
CREATE INDEX IF NOT EXISTS investment_rec_idx
    ON gptbridge_trading.investment_recommendation
    (instrument_id, status, created_at DESC);

-- ============================================================================
-- recommendation_version — immutable history (never overwritten)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.recommendation_version (
    recommendation_id text NOT NULL
        REFERENCES gptbridge_trading.investment_recommendation(recommendation_id),
    version           int NOT NULL,
    module_id         text NOT NULL DEFAULT 'ai-assistant',
    snapshot          jsonb NOT NULL,
    change_reason     text,
    changed_by        text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (recommendation_id, version)
);

-- ============================================================================
-- recommendation_outcome — ai/simulated/real kept strictly separate
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.recommendation_outcome (
    outcome_id         text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    recommendation_id  text NOT NULL
        REFERENCES gptbridge_trading.investment_recommendation(recommendation_id),
    outcome_kind       text NOT NULL,    -- ai_analysis|simulated|real
    horizon_days       int,
    price_at_recommendation numeric,
    price_at_evaluation     numeric,
    return_pct         numeric,
    max_favorable_pct  numeric,
    max_adverse_pct    numeric,
    signal_valid       boolean,
    conditions_held    boolean,
    evaluated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (outcome_id)
);
CREATE INDEX IF NOT EXISTS recommendation_outcome_idx
    ON gptbridge_trading.recommendation_outcome
    (recommendation_id, outcome_kind);

-- ============================================================================
-- model_analysis_record — per-inference provenance
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.model_analysis_record (
    record_id      text NOT NULL,
    module_id      text NOT NULL DEFAULT 'ai-assistant',
    run_id         text,
    task_kind      text NOT NULL,
    model_id       text NOT NULL,
    model_version  text,
    prompt_hash    text,
    tokens_in      int NOT NULL DEFAULT 0,
    tokens_out     int NOT NULL DEFAULT 0,
    latency_ms     numeric,
    status         text NOT NULL DEFAULT 'ok',   -- ok|degraded|failed|skipped
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (record_id)
);

-- ============================================================================
-- analysis_schedule — calendar-driven slots
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.analysis_schedule (
    schedule_id       text NOT NULL,
    module_id         text NOT NULL DEFAULT 'ai-assistant',
    market            text NOT NULL,     -- tw|us|fund|portfolio
    slot              text NOT NULL,     -- pre_open|intraday|post_close|nav_update|daily|weekly|monthly
    enabled           boolean NOT NULL DEFAULT true,
    last_run_at       timestamptz,
    last_market_date  date,
    run_count         int NOT NULL DEFAULT 0,
    PRIMARY KEY (schedule_id)
);

-- ============================================================================
-- RLS + grants (008/134/135/136 pattern)
-- ============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'analysis_run', 'analysis_evidence', 'investment_recommendation',
        'recommendation_version', 'recommendation_outcome',
        'model_analysis_record', 'analysis_schedule'
    ]
    LOOP
        EXECUTE format(
            'ALTER TABLE gptbridge_trading.%I ENABLE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'ALTER TABLE gptbridge_trading.%I FORCE ROW LEVEL SECURITY', t);
        EXECUTE format(
            'DROP POLICY IF EXISTS %I_read ON gptbridge_trading.%I', t, t);
        EXECUTE format(
            'CREATE POLICY %I_read ON gptbridge_trading.%I
             FOR SELECT USING (gptbridge_security.can_read(module_id))', t, t);
        EXECUTE format(
            'DROP POLICY IF EXISTS %I_write ON gptbridge_trading.%I', t, t);
        EXECUTE format(
            'CREATE POLICY %I_write ON gptbridge_trading.%I
             FOR ALL
             USING (gptbridge_security.can_write(module_id))
             WITH CHECK (
                 gptbridge_security.can_write(module_id)
                 AND module_id = current_setting(''app.current_module_id'', true)
             )', t, t);
        EXECUTE format(
            'REVOKE ALL ON gptbridge_trading.%I FROM PUBLIC', t);
        EXECUTE format(
            'GRANT SELECT ON gptbridge_trading.%I TO gptbridge_index_reader', t);
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_trading.%I TO gptbridge_index_executor', t);
    END LOOP;
END $$;
