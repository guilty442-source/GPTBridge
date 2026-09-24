-- 136_fund_data.sql
-- 星澄 AI 投資管理與自動操盤系統 — mutual fund domain schema.
--
-- Fund body vs share class are distinct identities; NAV keeps
-- published/estimated/confirmed separate; transactions use an explicit
-- status machine; costs/performance/recommendations are derived or
-- advisory records — fund live trading is disabled this phase.
-- Same RLS/module-scope pattern as 134/135.

CREATE SCHEMA IF NOT EXISTS gptbridge_trading;

-- ============================================================================
-- fund — fund body (shared across share classes)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund (
    fund_id          text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    fund_name        text NOT NULL,
    isin             text,
    fund_company     text,
    fund_type        text NOT NULL DEFAULT 'other',   -- equity|bond|balanced|multi_asset|money_market|target_date|index|other
    domicile         text,
    base_currency    text,
    inception_date   date,
    status           text NOT NULL DEFAULT 'active',
    -- classification with provenance (never name-derived)
    region           text,          -- taiwan|us|global|emerging|single_country|sector|thematic
    sector           text,
    theme            text,
    class_source_id  text,
    class_source_version text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fund_id)
);

-- ============================================================================
-- fund_share_class — tradable identity (fund:{id}:{class}:{ccy})
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_share_class (
    instrument_id        text NOT NULL,  -- fund:FUND:CLASS:CCY
    module_id            text NOT NULL DEFAULT 'ai-assistant',
    fund_id              text NOT NULL REFERENCES gptbridge_trading.fund(fund_id),
    share_class_id       text NOT NULL,
    share_class_name     text,
    share_class_currency text,
    distribution_policy  text NOT NULL DEFAULT 'accumulation', -- accumulation|distribution|both
    hedged_currency      text,
    status               text NOT NULL DEFAULT 'active',
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id),
    UNIQUE (fund_id, share_class_id, share_class_currency)
);

-- ============================================================================
-- fund_nav — published/estimated/confirmed kept distinct; corrections
-- supersede via revision (full history in the row, old kept via trigger-free
-- audit: superseded rows carry data_status='corrected')
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_nav (
    fund_id        text NOT NULL,
    share_class_id text NOT NULL,
    nav_date       date NOT NULL,
    nav_type       text NOT NULL DEFAULT 'published',  -- published|estimated|confirmed
    module_id      text NOT NULL DEFAULT 'ai-assistant',
    nav            numeric NOT NULL,
    currency       text NOT NULL,
    published_at   timestamptz,
    received_at    timestamptz NOT NULL DEFAULT now(),
    source_id      text NOT NULL,
    revision       int NOT NULL DEFAULT 1,
    data_status    text NOT NULL DEFAULT 'ok',
    PRIMARY KEY (fund_id, share_class_id, nav_date, nav_type)
);
CREATE INDEX IF NOT EXISTS fund_nav_lookup_idx
    ON gptbridge_trading.fund_nav (fund_id, share_class_id, nav_date DESC);

-- ============================================================================
-- fund_distribution — income / principal (return of capital) / unconfirmed
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_distribution (
    distribution_id      text NOT NULL,
    module_id            text NOT NULL DEFAULT 'ai-assistant',
    fund_id              text NOT NULL,
    share_class_id       text NOT NULL,
    ex_distribution_date date NOT NULL,
    payment_date         date,
    amount_per_unit      numeric NOT NULL,
    currency             text NOT NULL,
    distribution_source  text NOT NULL DEFAULT 'unconfirmed', -- income|principal|unconfirmed
    frequency            text,
    source_id            text NOT NULL,
    created_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (distribution_id),
    UNIQUE (share_class_id, ex_distribution_date, amount_per_unit)
);

-- ============================================================================
-- fund_fee — scoped fee rules (fund/class/platform/date/holding/currency)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_fee (
    fee_id           text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    fund_id          text NOT NULL,
    share_class_id   text NOT NULL,
    kind             text NOT NULL,   -- subscription|redemption|short_term|management|custody|platform|fx|other
    calc             text NOT NULL,   -- fixed|percent|holding_days|tiered
    rate             numeric NOT NULL DEFAULT 0,
    currency         text,
    platform         text,
    min_holding_days int,
    tiers            jsonb,
    effective_from   date,
    effective_to     date,
    nav_embedded     boolean NOT NULL DEFAULT false,
    source_id        text NOT NULL,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fee_id)
);
CREATE INDEX IF NOT EXISTS fund_fee_scope_idx
    ON gptbridge_trading.fund_fee (fund_id, share_class_id, kind);

-- ============================================================================
-- fund_holding — disclosed holdings with as_of + coverage
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_holding (
    fund_id      text NOT NULL,
    holding_id   text NOT NULL,     -- instrument_id / isin / label
    as_of_date   date NOT NULL,
    module_id    text NOT NULL DEFAULT 'ai-assistant',
    name         text,
    weight       numeric NOT NULL,
    asset_kind   text NOT NULL DEFAULT 'equity',
    country      text,
    sector       text,
    source_id    text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fund_id, holding_id, as_of_date)
);
CREATE INDEX IF NOT EXISTS fund_holding_idx
    ON gptbridge_trading.fund_holding (fund_id, as_of_date DESC);

-- ============================================================================
-- fund_exposure — aggregated exposure snapshots (country/sector/asset)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_exposure (
    exposure_id    text NOT NULL,
    module_id      text NOT NULL DEFAULT 'ai-assistant',
    fund_id        text NOT NULL,
    as_of_date     date NOT NULL,
    dimension      text NOT NULL,   -- country|sector|asset_kind|theme
    name           text NOT NULL,
    weight         numeric NOT NULL,
    coverage_ratio numeric,
    source_id      text NOT NULL,
    data_status    text NOT NULL DEFAULT 'ok',
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (exposure_id)
);

-- ============================================================================
-- fund_transaction — explicit status machine; SETTLED only credits
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_transaction (
    transaction_id    text NOT NULL,
    module_id         text NOT NULL DEFAULT 'ai-assistant',
    account_id        text NOT NULL,
    fund_id           text NOT NULL,
    share_class_id    text NOT NULL,
    transaction_type  text NOT NULL,  -- subscribe|add|redeem|partial_redeem|switch|recurring|distribution|reinvest
    application_date  date,
    pricing_date      date,
    confirmation_date date,
    settlement_date   date,
    amount            numeric NOT NULL,
    units             numeric NOT NULL DEFAULT 0,
    confirmed_nav     numeric,
    currency          text NOT NULL,
    fees              numeric NOT NULL DEFAULT 0,
    status            text NOT NULL DEFAULT 'DRAFT',
    source_id         text NOT NULL,
    note              text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (transaction_id)
);
CREATE INDEX IF NOT EXISTS fund_txn_idx
    ON gptbridge_trading.fund_transaction (account_id, fund_id, status);

-- ============================================================================
-- fund_cost_basis — per account+class rolling basis snapshot
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_cost_basis (
    account_id       text NOT NULL,
    fund_id          text NOT NULL,
    share_class_id   text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    units            numeric NOT NULL DEFAULT 0,
    invested_principal numeric NOT NULL DEFAULT 0,
    avg_cost_per_unit numeric,
    realized_pnl     numeric NOT NULL DEFAULT 0,
    cash_dividends_received numeric NOT NULL DEFAULT 0,
    method           text NOT NULL DEFAULT 'weighted_average',
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, fund_id, share_class_id)
);

-- ============================================================================
-- fund_performance — computed period results (cache of derivations)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_performance (
    fund_id        text NOT NULL,
    share_class_id text NOT NULL,
    period         text NOT NULL,
    basis          text NOT NULL,      -- raw|with_distributions|net_of_fees
    module_id      text NOT NULL DEFAULT 'ai-assistant',
    return_pct     numeric,
    annualized     numeric,
    volatility     numeric,
    max_drawdown   numeric,
    sharpe         numeric,
    sortino        numeric,
    data_points    int,
    computed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fund_id, share_class_id, period, basis)
);

-- ============================================================================
-- fund_recommendation — AI advice with full provenance (advisory only)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_recommendation (
    recommendation_id text NOT NULL,
    module_id         text NOT NULL DEFAULT 'ai-assistant',
    fund_id           text NOT NULL,
    share_class_id    text NOT NULL,
    account_id        text NOT NULL,
    recommendation_type text NOT NULL, -- SUBSCRIBE|ADD|HOLD|REDUCE|REDEEM|SWITCH
    analysis_date     date NOT NULL,
    nav_date          date,             -- NAV basis — explicit
    investment_horizon text,
    risk_factors      jsonb,
    reasoning         text,
    data_sources      jsonb,
    suggested_amount  numeric,
    suggested_weight  numeric,
    reevaluate_when   jsonb,
    model_id          text,
    model_version     text,
    strategy_version  text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (recommendation_id)
);

-- ============================================================================
-- fund_strategy — versioned strategies
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_strategy (
    strategy_id      text NOT NULL,
    strategy_version text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    kind             text NOT NULL,
    parameters       jsonb,
    status           text NOT NULL DEFAULT 'active',
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, strategy_version)
);

-- ============================================================================
-- fund_sync_state — incremental import/provider cursors
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_sync_state (
    source_id     text NOT NULL,
    fund_id       text NOT NULL,
    data_kind     text NOT NULL,     -- nav|distribution|holding|fee
    module_id     text NOT NULL DEFAULT 'ai-assistant',
    cursor        timestamptz,
    last_sync_at  timestamptz NOT NULL DEFAULT now(),
    status        text NOT NULL DEFAULT 'ok',
    error_code    text,
    rows_written  int NOT NULL DEFAULT 0,
    PRIMARY KEY (source_id, fund_id, data_kind)
);

-- ============================================================================
-- RLS + grants (008/134/135 pattern)
-- ============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'fund', 'fund_share_class', 'fund_nav', 'fund_distribution',
        'fund_fee', 'fund_holding', 'fund_exposure', 'fund_transaction',
        'fund_cost_basis', 'fund_performance', 'fund_recommendation',
        'fund_strategy', 'fund_sync_state'
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
