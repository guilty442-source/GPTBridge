-- 139_simulation_trading.sql
-- AI 影子操盤 + PAPER 模擬交易 — dedicated simulation schema.
--
-- All rows in this migration are SIMULATED by construction:
-- paper_* ids live in a separate namespace, every execution carries
-- simulated=true (enforced by CHECK), and no paper_* row may ever be
-- written into the formal broker/account/position tables (134) or
-- backtest tables (138). SHADOW signal outcomes, PAPER fills, and
-- BACKTEST results are three distinct record families — never merged.

CREATE SCHEMA IF NOT EXISTS gptbridge_trading;

-- ============================================================================
-- paper_account — virtual accounts (paper-* ids only, never real broker ids)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.paper_account (
    account_id       text NOT NULL CHECK (account_id LIKE 'paper-%'),
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    account_name     text NOT NULL,
    market           text NOT NULL,    -- TAIWAN_EQUITY|US_EQUITY|MUTUAL_FUND
    base_currency    text NOT NULL,
    initial_capital  numeric NOT NULL DEFAULT 0,
    status           text NOT NULL DEFAULT 'ACTIVE', -- ACTIVE|SUSPENDED|CLOSED
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id)
);

-- ============================================================================
-- paper_cash_ledger — append-only cash journal; balances are derived,
-- never mutated in place (AVAILABLE|RESERVED|UNSETTLED legs)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.paper_cash_ledger (
    entry_id    text NOT NULL,
    module_id   text NOT NULL DEFAULT 'ai-assistant',
    account_id  text NOT NULL REFERENCES gptbridge_trading.paper_account(account_id),
    kind        text NOT NULL,         -- initial|deposit|withdraw|buy_debit|
                                       -- sell_credit|fee|tax|dividend|
                                       -- adjustment|reserve|release|transfer|fx_convert
    amount      numeric NOT NULL,
    state       text NOT NULL,         -- AVAILABLE|RESERVED|UNSETTLED|SETTLED
    ref         text NOT NULL DEFAULT '',  -- order_id|exec_id|event id
    settle_on   date,                  -- UNSETTLED -> available date (T+n)
    at          timestamptz NOT NULL DEFAULT now(),
    simulated   boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (entry_id)
);
CREATE INDEX IF NOT EXISTS paper_cash_ledger_idx
    ON gptbridge_trading.paper_cash_ledger (account_id, ref, at);

-- ============================================================================
-- paper_position — virtual holdings; change only via confirmed sim fills
-- or controlled corporate-adjustment events (paper_position.event column)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.paper_position (
    account_id     text NOT NULL REFERENCES gptbridge_trading.paper_account(account_id),
    instrument_id  text NOT NULL,
    module_id      text NOT NULL DEFAULT 'ai-assistant',
    quantity       numeric NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    average_cost   numeric NOT NULL DEFAULT 0,
    realized_pnl   numeric NOT NULL DEFAULT 0,
    currency       text NOT NULL DEFAULT '',
    updated_at     timestamptz NOT NULL DEFAULT now(),
    simulated      boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (account_id, instrument_id)
);

-- ============================================================================
-- paper_order — simulated order book; legal state machine enforced in code
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.paper_order (
    order_id          text NOT NULL,
    module_id         text NOT NULL DEFAULT 'ai-assistant',
    account_id        text NOT NULL REFERENCES gptbridge_trading.paper_account(account_id),
    client_order_id   text NOT NULL DEFAULT '',   -- idempotency key
    instrument_id     text NOT NULL,
    strategy_id       text NOT NULL DEFAULT '',
    strategy_version  int  NOT NULL DEFAULT 0,
    source_signal_id  text NOT NULL DEFAULT '',
    side              text NOT NULL,              -- buy|sell|subscribe|redeem
    order_type        text NOT NULL DEFAULT 'market', -- market|limit|stop|stop_limit|recurring
    quantity          numeric NOT NULL,
    filled_qty        numeric NOT NULL DEFAULT 0,
    avg_fill_price    numeric NOT NULL DEFAULT 0,
    limit_price       numeric,
    stop_price        numeric,
    reference_price   numeric,
    currency          text NOT NULL DEFAULT '',
    status            text NOT NULL DEFAULT 'CREATED', -- CREATED|VALIDATED|ACCEPTED|
                                -- PARTIALLY_FILLED|FILLED|CANCELLED|EXPIRED|REJECTED
    rejection_reason  text NOT NULL DEFAULT '',
    expires_at        timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    simulated         boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (order_id),
    UNIQUE (account_id, client_order_id)  -- caller dedup per account
);
CREATE INDEX IF NOT EXISTS paper_order_idx
    ON gptbridge_trading.paper_order (account_id, status, created_at DESC);

-- ============================================================================
-- paper_execution — simulated fills only (simulated=true enforced)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.paper_execution (
    exec_id        text NOT NULL,
    module_id      text NOT NULL DEFAULT 'ai-assistant',
    order_id       text NOT NULL REFERENCES gptbridge_trading.paper_order(order_id),
    account_id     text NOT NULL REFERENCES gptbridge_trading.paper_account(account_id),
    event_seq      int  NOT NULL DEFAULT 0,    -- simulation event sequence
    instrument_id  text NOT NULL,
    side           text NOT NULL,
    quantity       numeric NOT NULL,
    price          numeric NOT NULL,
    currency       text NOT NULL DEFAULT '',
    commission     numeric NOT NULL DEFAULT 0,
    tax            numeric NOT NULL DEFAULT 0,
    slippage       numeric NOT NULL DEFAULT 0,
    spread         numeric NOT NULL DEFAULT 0,
    market_source  text NOT NULL DEFAULT '',   -- data source id
    data_revision  text NOT NULL DEFAULT '',
    exec_model     text NOT NULL DEFAULT 'daily_bar', -- daily_bar|orderbook|nav
    assumptions    jsonb NOT NULL DEFAULT '[]',
    created_at     timestamptz NOT NULL DEFAULT now(),
    simulated      boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (exec_id)
);
CREATE INDEX IF NOT EXISTS paper_execution_idx
    ON gptbridge_trading.paper_execution (account_id, order_id, created_at);

-- ============================================================================
-- paper_portfolio_snapshot — periodic marked-to-market snapshots
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.paper_portfolio_snapshot (
    snapshot_id      text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    account_id       text NOT NULL REFERENCES gptbridge_trading.paper_account(account_id),
    taken_at         timestamptz NOT NULL DEFAULT now(),
    cash_available   numeric NOT NULL DEFAULT 0,
    cash_reserved    numeric NOT NULL DEFAULT 0,
    cash_unsettled   numeric NOT NULL DEFAULT 0,
    positions_value  numeric NOT NULL DEFAULT 0,
    total_assets     numeric NOT NULL DEFAULT 0,
    marks            jsonb NOT NULL DEFAULT '{}',
    simulated        boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (snapshot_id)
);
CREATE INDEX IF NOT EXISTS paper_portfolio_snapshot_idx
    ON gptbridge_trading.paper_portfolio_snapshot (account_id, taken_at DESC);

-- ============================================================================
-- paper_strategy_run — strategy↔paper-account binding under the coordinator
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.paper_strategy_run (
    run_id            text NOT NULL,
    module_id         text NOT NULL DEFAULT 'ai-assistant',
    strategy_id       text NOT NULL,
    strategy_version  int  NOT NULL,
    paper_account_id  text NOT NULL REFERENCES gptbridge_trading.paper_account(account_id),
    allocated_capital numeric NOT NULL DEFAULT 0,
    risk_budget       numeric NOT NULL DEFAULT 0,
    status            text NOT NULL DEFAULT 'running', -- running|paused|stopped|error
    started_at        timestamptz NOT NULL DEFAULT now(),
    stopped_at        timestamptz,
    simulated         boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (run_id)
);
CREATE INDEX IF NOT EXISTS paper_strategy_run_idx
    ON gptbridge_trading.paper_strategy_run (strategy_id, status);

-- ============================================================================
-- paper_performance — PAPER-only performance rows (never real performance)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.paper_performance (
    perf_id          text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    account_id       text NOT NULL REFERENCES gptbridge_trading.paper_account(account_id),
    window           text NOT NULL DEFAULT 'all',   -- all|1d|mtd|…
    initial_capital  numeric NOT NULL,
    total_assets     numeric NOT NULL,
    realized_pnl     numeric NOT NULL DEFAULT 0,
    unrealized_pnl   numeric NOT NULL DEFAULT 0,
    total_return     numeric,
    annualized_return numeric,
    max_drawdown     numeric,
    volatility       numeric,
    sharpe           numeric,
    sortino          numeric,
    win_rate         numeric,
    profit_factor    text,
    turnover         numeric,
    cost_ratio       numeric,
    cost_total       numeric NOT NULL DEFAULT 0,
    dividends        numeric NOT NULL DEFAULT 0,
    attribution      jsonb NOT NULL DEFAULT '{}',   -- strategy/market/currency
    computed_at      timestamptz NOT NULL DEFAULT now(),
    label            text NOT NULL DEFAULT 'PAPER PERFORMANCE',
    simulated        boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (perf_id)
);
CREATE INDEX IF NOT EXISTS paper_performance_idx
    ON gptbridge_trading.paper_performance (account_id, computed_at DESC);

-- ============================================================================
-- shadow_signal — immutable AI signal records; originals never rewritten
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.shadow_signal (
    signal_id        text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    instrument_id    text NOT NULL,
    market           text NOT NULL,
    strategy_id      text NOT NULL DEFAULT '',
    strategy_version int  NOT NULL DEFAULT 0,
    model_id         text NOT NULL DEFAULT '',
    model_version    text NOT NULL DEFAULT '',
    side             text NOT NULL,     -- BUY|SELL|HOLD|ADD|REDUCE|EXIT|
                                        -- SUBSCRIBE|REDEEM|SWITCH
    reference_price  numeric,
    data_revision    text NOT NULL DEFAULT '',
    evidence_refs    jsonb NOT NULL DEFAULT '[]',
    risk_note        text NOT NULL DEFAULT '',
    valid_until      text NOT NULL DEFAULT '',
    created_at       timestamptz NOT NULL DEFAULT now(),
    simulated        boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (signal_id)
);
CREATE INDEX IF NOT EXISTS shadow_signal_idx
    ON gptbridge_trading.shadow_signal (instrument_id, created_at DESC);

-- ============================================================================
-- shadow_signal_outcome — post-signal market path (MFE/MAE/horizons);
-- explicitly NOT traded performance
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.shadow_signal_outcome (
    outcome_id       text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    signal_id        text NOT NULL REFERENCES gptbridge_trading.shadow_signal(signal_id),
    instrument_id    text NOT NULL,
    basis            text NOT NULL DEFAULT 'close',  -- close|nav
    reference_price  numeric NOT NULL,
    horizons         jsonb NOT NULL DEFAULT '{}',    -- {'1': {...}, '5': ...}
    evaluated_at     timestamptz NOT NULL DEFAULT now(),
    note             text NOT NULL DEFAULT '訊號追蹤非成交績效',
    simulated        boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (outcome_id)
);
CREATE INDEX IF NOT EXISTS shadow_signal_outcome_idx
    ON gptbridge_trading.shadow_signal_outcome (signal_id, evaluated_at DESC);

-- ============================================================================
-- shadow_paper_comparison — signal return vs simulated gross vs net
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.shadow_paper_comparison (
    comparison_id    text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    signal_id        text NOT NULL REFERENCES gptbridge_trading.shadow_signal(signal_id),
    signal_price     numeric NOT NULL,
    market_end_price numeric NOT NULL,
    signal_return    numeric,
    fills            jsonb NOT NULL DEFAULT '[]',  -- per-fill gross/net/slippage
    note             text NOT NULL DEFAULT '',
    computed_at      timestamptz NOT NULL DEFAULT now(),
    simulated        boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (comparison_id)
);

-- ============================================================================
-- simulation_event — sequenced event journal (recovery ordering)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.simulation_event (
    seq         bigint NOT NULL,
    module_id   text NOT NULL DEFAULT 'ai-assistant',
    kind        text NOT NULL,       -- paper_order|paper_fill|paper_cancel|
                                   -- shadow_signal|paper_account|paper_corporate|…
    detail      jsonb NOT NULL DEFAULT '{}',
    at          timestamptz NOT NULL DEFAULT now(),
    simulated   boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (seq)
);

-- ============================================================================
-- simulation_checkpoint — restart/recovery anchors
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.simulation_checkpoint (
    checkpoint_id  text NOT NULL,
    module_id      text NOT NULL DEFAULT 'ai-assistant',
    seq            bigint NOT NULL,          -- last committed event seq
    state          jsonb NOT NULL DEFAULT '{}',
    created_at     timestamptz NOT NULL DEFAULT now(),
    simulated      boolean NOT NULL DEFAULT true CHECK (simulated),
    PRIMARY KEY (checkpoint_id)
);
CREATE INDEX IF NOT EXISTS simulation_checkpoint_idx
    ON gptbridge_trading.simulation_checkpoint (seq DESC);

-- ============================================================================
-- RLS + grants (008/134/135/136/137/138 pattern)
-- ============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'paper_account', 'paper_cash_ledger', 'paper_position',
        'paper_order', 'paper_execution', 'paper_portfolio_snapshot',
        'paper_strategy_run', 'paper_performance',
        'shadow_signal', 'shadow_signal_outcome', 'shadow_paper_comparison',
        'simulation_event', 'simulation_checkpoint'
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
