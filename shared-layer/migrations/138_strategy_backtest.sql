-- 138_strategy_backtest.sql
-- 策略引擎與歷史回測系統 — strategy lifecycle + simulation schema.
--
-- Every backtest row is explicitly simulated: strategy versions are
-- immutable snapshots, results carry data_revision + assumptions, and
-- simulated trades NEVER land in broker execution / account / position
-- authoritative tables (134). LIVE_ELIGIBLE means validation passed —
-- it grants no execution authorization.

CREATE SCHEMA IF NOT EXISTS gptbridge_trading;

-- ============================================================================
-- strategy — registered strategy definitions (latest live row per id)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy (
    strategy_id      text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    strategy_name    text NOT NULL,
    strategy_type    text NOT NULL,    -- TREND_FOLLOWING|MEAN_REVERSION|MOMENTUM|BREAKOUT|MULTI_FACTOR|ASSET_ALLOCATION|PORTFOLIO_REBALANCING|FUND_RECURRING_INVESTMENT
    market           text NOT NULL,    -- TAIWAN_EQUITY|US_EQUITY|MUTUAL_FUND
    instrument_scope jsonb NOT NULL DEFAULT '[]',
    timeframe        text NOT NULL DEFAULT '1d',
    parameters       jsonb NOT NULL DEFAULT '{}',
    risk_profile     jsonb NOT NULL DEFAULT '{}',
    version          int  NOT NULL DEFAULT 1,
    status           text NOT NULL DEFAULT 'DRAFT',  -- DRAFT|TESTING|VALIDATED|SHADOW|PAPER|LIVE_ELIGIBLE|RETIRED
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id)
);
CREATE INDEX IF NOT EXISTS strategy_status_idx
    ON gptbridge_trading.strategy (status, market);

-- ============================================================================
-- strategy_version — immutable snapshots; released versions never mutate
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_version (
    snapshot_id        text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    strategy_id        text NOT NULL,
    version            int  NOT NULL,
    definition         jsonb NOT NULL,
    code_version       text NOT NULL DEFAULT '',
    data_version       text NOT NULL DEFAULT '',
    model_version      text NOT NULL DEFAULT '',
    risk_params        jsonb NOT NULL DEFAULT '{}',
    cost_assumptions   jsonb NOT NULL DEFAULT '{}',
    backtest_run_ids   jsonb NOT NULL DEFAULT '[]',
    validation_results jsonb NOT NULL DEFAULT '[]',
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_id),
    UNIQUE (strategy_id, version)
);

-- ============================================================================
-- strategy_parameter — append-only parameter-search audit trail
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_parameter (
    search_id     text NOT NULL,
    module_id     text NOT NULL DEFAULT 'ai-assistant',
    strategy_id   text NOT NULL,
    version       int  NOT NULL,
    parameters    jsonb NOT NULL DEFAULT '{}',
    data_window   text NOT NULL DEFAULT '',
    split         text NOT NULL,       -- in_sample|validation|out_of_sample|walk_forward
    metrics       jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (search_id)
);
CREATE INDEX IF NOT EXISTS strategy_parameter_idx
    ON gptbridge_trading.strategy_parameter (strategy_id, version, split);

-- ============================================================================
-- backtest_run — one replay job (queued/running/done/failed/cancelled)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.backtest_run (
    run_id            text NOT NULL,
    module_id         text NOT NULL DEFAULT 'ai-assistant',
    strategy_id       text NOT NULL,
    strategy_version  int  NOT NULL,
    market            text NOT NULL,
    instrument_scope  jsonb NOT NULL DEFAULT '[]',
    start_date        date NOT NULL,
    end_date          date NOT NULL,
    initial_capital   numeric NOT NULL,
    currency          text NOT NULL DEFAULT 'TWD',
    fee_model         text NOT NULL DEFAULT 'broker_default',
    slippage_model    text NOT NULL DEFAULT 'bps',
    execution_model   text NOT NULL DEFAULT 'next_open',
    data_revision     text NOT NULL DEFAULT 'latest',
    assumptions       jsonb NOT NULL DEFAULT '{}',
    environment       jsonb NOT NULL DEFAULT '{}',  -- tool/python/os markers
    status            text NOT NULL DEFAULT 'queued',
    simulated         boolean NOT NULL DEFAULT true,
    stale             boolean NOT NULL DEFAULT false,  -- source data revised
    created_at        timestamptz NOT NULL DEFAULT now(),
    finished_at       timestamptz,
    PRIMARY KEY (run_id)
);
CREATE INDEX IF NOT EXISTS backtest_run_idx
    ON gptbridge_trading.backtest_run (strategy_id, created_at DESC);

-- ============================================================================
-- backtest_result — metrics head; never a promise of future returns
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.backtest_result (
    run_id            text NOT NULL REFERENCES gptbridge_trading.backtest_run(run_id),
    module_id         text NOT NULL DEFAULT 'ai-assistant',
    initial_capital   numeric NOT NULL,
    final_equity      numeric NOT NULL,
    total_return      numeric,
    annualized_return numeric,
    max_drawdown      numeric,
    volatility        numeric,
    sharpe            numeric,
    sortino           numeric,
    trade_count       int  NOT NULL DEFAULT 0,
    win_rate          numeric,
    avg_win           numeric,
    avg_loss          numeric,
    profit_factor     text,            -- 'inf' representable
    cost_total        numeric NOT NULL DEFAULT 0,
    survivorship_risk boolean NOT NULL DEFAULT false,
    warnings          jsonb NOT NULL DEFAULT '[]',
    PRIMARY KEY (run_id)
);

-- ============================================================================
-- backtest_trade — simulated fills only (simulated=true enforced)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.backtest_trade (
    run_id        text NOT NULL REFERENCES gptbridge_trading.backtest_run(run_id),
    module_id     text NOT NULL DEFAULT 'ai-assistant',
    seq           int  NOT NULL,
    instrument_id text NOT NULL,
    side          text NOT NULL,
    quantity      numeric NOT NULL,
    price         numeric NOT NULL,
    fee           numeric NOT NULL DEFAULT 0,
    slippage      numeric NOT NULL DEFAULT 0,
    signal_bar    int  NOT NULL,
    fill_bar      int  NOT NULL,
    fill_time     timestamptz,
    order_type    text NOT NULL DEFAULT 'market',
    fill_kind     text NOT NULL DEFAULT 'full',   -- full|partial|none
    reason        text NOT NULL DEFAULT '',
    simulated     boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (run_id, seq)
);

-- ============================================================================
-- backtest_position — position-change journal
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.backtest_position (
    run_id        text NOT NULL REFERENCES gptbridge_trading.backtest_run(run_id),
    module_id     text NOT NULL DEFAULT 'ai-assistant',
    seq           int  NOT NULL,
    instrument_id text NOT NULL,
    event         text NOT NULL,       -- fill|rebalance_marker|corporate_action
    quantity      numeric,
    cash          numeric,
    bar           int,
    PRIMARY KEY (run_id, seq)
);

-- ============================================================================
-- backtest_equity_curve — chart-ready equity/cash marks
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.backtest_equity_curve (
    run_id          text NOT NULL REFERENCES gptbridge_trading.backtest_run(run_id),
    module_id       text NOT NULL DEFAULT 'ai-assistant',
    t               date NOT NULL,
    equity          numeric NOT NULL,
    cash            numeric NOT NULL,
    positions_value numeric NOT NULL DEFAULT 0,
    PRIMARY KEY (run_id, t)
);

-- ============================================================================
-- backtest_metric — extensible per-run metrics (monthly/annual returns…)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.backtest_metric (
    run_id     text NOT NULL REFERENCES gptbridge_trading.backtest_run(run_id),
    module_id  text NOT NULL DEFAULT 'ai-assistant',
    metric     text NOT NULL,          -- monthly_return.2026-01|annual.2026|…
    value      numeric,
    detail     jsonb NOT NULL DEFAULT '{}',
    PRIMARY KEY (run_id, metric)
);

-- ============================================================================
-- strategy_validation — IS/validation/OOS/walk-forward records
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_validation (
    validation_id text NOT NULL,
    module_id     text NOT NULL DEFAULT 'ai-assistant',
    strategy_id   text NOT NULL,
    version       int  NOT NULL,
    split         text NOT NULL,       -- in_sample|validation|out_of_sample|walk_forward
    window        text NOT NULL,
    run_id        text,
    metrics       jsonb NOT NULL DEFAULT '{}',
    flags         jsonb NOT NULL DEFAULT '[]',  -- OOS_REUSED|NARROW_PROFIT_ISLAND
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (validation_id)
);
CREATE INDEX IF NOT EXISTS strategy_validation_idx
    ON gptbridge_trading.strategy_validation (strategy_id, version);

-- ============================================================================
-- strategy_research — AI interpretation/proposal log (advisory only)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_research (
    research_id   text NOT NULL,
    module_id     text NOT NULL DEFAULT 'ai-assistant',
    strategy_id   text,
    run_id        text,
    question      text NOT NULL DEFAULT '',
    interpretation text NOT NULL DEFAULT '',
    model_id      text,
    model_version text,
    degraded      boolean NOT NULL DEFAULT false,
    proposed_draft jsonb,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (research_id)
);

-- ============================================================================
-- strategy_comparison — comparison runs with comparability metadata
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_comparison (
    comparison_id    text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    run_ids          jsonb NOT NULL DEFAULT '[]',
    comparable       boolean NOT NULL DEFAULT false,
    mismatched_basis jsonb NOT NULL DEFAULT '[]',
    table            jsonb NOT NULL DEFAULT '[]',
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (comparison_id)
);

-- ============================================================================
-- RLS + grants (008/134/135/136/137 pattern)
-- ============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'strategy', 'strategy_version', 'strategy_parameter',
        'backtest_run', 'backtest_result', 'backtest_trade',
        'backtest_position', 'backtest_equity_curve', 'backtest_metric',
        'strategy_validation', 'strategy_research', 'strategy_comparison'
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
