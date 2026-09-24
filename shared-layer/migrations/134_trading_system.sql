-- 134_trading_system.sql
-- 星澄 AI 投資管理與自動操盤系統 — canonical trading schema.
--
-- Authoritative records for instruments, brokers, accounts, portfolios,
-- positions, cash balances, market observations, the full trading
-- pipeline (signal → proposal → risk_decision → order_request →
-- order_receipt → execution), fund NAV/transactions, strategy versions
-- and the append-only audit trail. The investment-mobile engine journal
-- is a runtime mirror; these tables are the canonical store owned by
-- ai-assistant (module_id = 'ai-assistant').
--
-- Codex basis:
--   A46/E22 — audit: mandatory-ledger (append-only audit_event).
--   A49/E35 + A44/E30 — RLS + module-scoped writes (008 pattern).
--   A8/E21  — Qdrant canonical semantic index (financial-research
--             collection registered in qdrant_generation; Qdrant never
--             holds authoritative account/position/trade data).

CREATE SCHEMA IF NOT EXISTS gptbridge_trading;

-- ============================================================================
-- instrument — unified tradable product model
--   identity: "market:type:symbol:currency" or "fund:fund_id:share:currency"
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.instrument (
    instrument_id      text        NOT NULL,
    module_id          text        NOT NULL DEFAULT 'ai-assistant',
    instrument_type    text        NOT NULL,  -- TW_STOCK|TW_ETF|US_STOCK|US_ETF|MUTUAL_FUND
    market             text        NOT NULL,  -- tw | us | fund
    symbol             text        NOT NULL,
    isin               text,
    currency           text        NOT NULL,
    display_name       text,
    exchange           text,
    trading_calendar   text,
    price_precision    int         NOT NULL DEFAULT 2,
    quantity_precision int         NOT NULL DEFAULT 0,
    status             text        NOT NULL DEFAULT 'active',
    -- fund-only extension (NULL for exchange-traded)
    fund_id            text,
    fund_share_class   text,
    fund_currency      text,
    distribution_type  text,
    nav                numeric,
    nav_date           date,
    subscription_cutoff text,
    redemption_rules   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    fee_schedule       jsonb       NOT NULL DEFAULT '{}'::jsonb,
    meta               jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id),
    UNIQUE (market, instrument_type, symbol, currency)
);

-- ============================================================================
-- broker + account — per-broker isolation of funds/positions/orders
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.broker (
    broker_id      text        NOT NULL,  -- CATHAY_SECURITIES|FUBON_SUBBROKERAGE|MUTUAL_FUND_PROVIDER
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    market         text        NOT NULL,
    label          text,
    api_verified   boolean     NOT NULL DEFAULT false,
    verified_api   text,                   -- official API identity once verified
    verified_at    timestamptz,
    capabilities   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (broker_id)
);

CREATE TABLE IF NOT EXISTS gptbridge_trading.account (
    account_id    text        NOT NULL,
    module_id     text        NOT NULL DEFAULT 'ai-assistant',
    broker_id     text        NOT NULL REFERENCES gptbridge_trading.broker(broker_id),
    market        text        NOT NULL,
    currency      text        NOT NULL,
    permissions   jsonb       NOT NULL DEFAULT '[]'::jsonb,  -- analysis|shadow|paper|live|manual-import
    status        text        NOT NULL DEFAULT 'active',
    label         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id)
);
CREATE INDEX IF NOT EXISTS account_broker_idx
    ON gptbridge_trading.account (broker_id);

-- ============================================================================
-- portfolio + position + cash_balance — asset state per account
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.portfolio (
    portfolio_id  text        NOT NULL,
    module_id     text        NOT NULL DEFAULT 'ai-assistant',
    account_id    text        NOT NULL REFERENCES gptbridge_trading.account(account_id),
    label         text,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (portfolio_id),
    UNIQUE (account_id)
);

CREATE TABLE IF NOT EXISTS gptbridge_trading.position (
    position_id    text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    account_id     text        NOT NULL REFERENCES gptbridge_trading.account(account_id),
    instrument_id  text        NOT NULL REFERENCES gptbridge_trading.instrument(instrument_id),
    quantity       numeric     NOT NULL DEFAULT 0,
    average_cost   numeric     NOT NULL DEFAULT 0,
    last_price     numeric,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (position_id),
    UNIQUE (account_id, instrument_id)
);

CREATE TABLE IF NOT EXISTS gptbridge_trading.cash_balance (
    account_id    text        NOT NULL REFERENCES gptbridge_trading.account(account_id),
    module_id     text        NOT NULL DEFAULT 'ai-assistant',
    currency      text        NOT NULL,
    available     numeric     NOT NULL DEFAULT 0,
    held          numeric     NOT NULL DEFAULT 0,
    simulated     boolean     NOT NULL DEFAULT false,
    updated_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, currency)
);

-- ============================================================================
-- market_observation — quote/NAV intake (non-authoritative cache mirror)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.market_observation (
    observation_id text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    instrument_id  text        NOT NULL REFERENCES gptbridge_trading.instrument(instrument_id),
    price          numeric     NOT NULL,
    currency       text        NOT NULL,
    kind           text        NOT NULL DEFAULT 'quote',  -- quote | nav
    source         text        NOT NULL DEFAULT 'xingcheng',
    observed_at    timestamptz NOT NULL DEFAULT now(),
    payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (observation_id)
);
CREATE INDEX IF NOT EXISTS market_observation_latest_idx
    ON gptbridge_trading.market_observation (instrument_id, observed_at DESC);

-- ============================================================================
-- trading pipeline — signal → proposal → risk_decision → order_request
--                     → order_receipt → execution
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trading_signal (
    signal_id      text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    instrument_id  text        NOT NULL,
    market         text        NOT NULL,
    side           text        NOT NULL,  -- buy|sell|subscribe|redeem
    confidence     numeric,
    price          numeric,
    quantity       numeric,
    rationale      text,
    source         text        NOT NULL DEFAULT 'xingcheng',
    payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_id)
);
CREATE INDEX IF NOT EXISTS trading_signal_market_idx
    ON gptbridge_trading.trading_signal (market, created_at DESC);

CREATE TABLE IF NOT EXISTS gptbridge_trading.trade_proposal (
    proposal_id    text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    signal_id      text        REFERENCES gptbridge_trading.trading_signal(signal_id),
    strategy_id    text,
    account_id     text        REFERENCES gptbridge_trading.account(account_id),
    instrument_id  text        NOT NULL,
    market         text        NOT NULL,
    side           text        NOT NULL,
    quantity       numeric     NOT NULL,
    price          numeric,
    notional       numeric,
    risk_params    jsonb       NOT NULL DEFAULT '{}'::jsonb,  -- informational only
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (proposal_id)
);

CREATE TABLE IF NOT EXISTS gptbridge_trading.risk_decision (
    decision_id    text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    proposal_id    text        REFERENCES gptbridge_trading.trade_proposal(proposal_id),
    approved       boolean     NOT NULL,
    reasons        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    limits_checked jsonb       NOT NULL DEFAULT '[]'::jsonb,
    backend        text        NOT NULL DEFAULT 'python',  -- python | native:risk_core
    evaluated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (decision_id)
);
CREATE INDEX IF NOT EXISTS risk_decision_proposal_idx
    ON gptbridge_trading.risk_decision (proposal_id);

CREATE TABLE IF NOT EXISTS gptbridge_trading.order_request (
    order_id       text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    proposal_id    text        NOT NULL REFERENCES gptbridge_trading.trade_proposal(proposal_id),
    decision_id    text        REFERENCES gptbridge_trading.risk_decision(decision_id),
    account_id     text        NOT NULL REFERENCES gptbridge_trading.account(account_id),
    status         text        NOT NULL DEFAULT 'created',
    mode           text        NOT NULL DEFAULT 'ANALYSIS',  -- mode at submission
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (order_id)
);
CREATE INDEX IF NOT EXISTS order_request_status_idx
    ON gptbridge_trading.order_request (status, created_at DESC);

CREATE TABLE IF NOT EXISTS gptbridge_trading.order_receipt (
    receipt_id       text        NOT NULL,
    module_id        text        NOT NULL DEFAULT 'ai-assistant',
    order_id         text        NOT NULL REFERENCES gptbridge_trading.order_request(order_id),
    broker_order_id  text,
    status           text        NOT NULL DEFAULT 'submitted',
    rejection        text,
    simulated        boolean     NOT NULL DEFAULT true,
    received_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (receipt_id)
);
CREATE INDEX IF NOT EXISTS order_receipt_order_idx
    ON gptbridge_trading.order_receipt (order_id);

CREATE TABLE IF NOT EXISTS gptbridge_trading.execution (
    execution_id   text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    order_id       text        NOT NULL REFERENCES gptbridge_trading.order_request(order_id),
    account_id     text        NOT NULL REFERENCES gptbridge_trading.account(account_id),
    instrument_id  text        NOT NULL,
    market         text        NOT NULL,
    side           text        NOT NULL,
    quantity       numeric     NOT NULL,
    price          numeric     NOT NULL,
    commission     numeric     NOT NULL DEFAULT 0,
    fees           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    simulated      boolean     NOT NULL DEFAULT true,  -- never masquerade
    executed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (execution_id)
);
CREATE INDEX IF NOT EXISTS execution_order_idx
    ON gptbridge_trading.execution (order_id);

CREATE TABLE IF NOT EXISTS gptbridge_trading.portfolio_snapshot (
    snapshot_id        text        NOT NULL,
    module_id          text        NOT NULL DEFAULT 'ai-assistant',
    account_id         text        NOT NULL REFERENCES gptbridge_trading.account(account_id),
    total_market_value numeric     NOT NULL DEFAULT 0,
    total_cash         numeric     NOT NULL DEFAULT 0,
    positions          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    cash               jsonb       NOT NULL DEFAULT '[]'::jsonb,
    taken_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_id)
);
CREATE INDEX IF NOT EXISTS portfolio_snapshot_idx
    ON gptbridge_trading.portfolio_snapshot (account_id, taken_at DESC);

-- ============================================================================
-- fund_nav + fund_transaction — mutual-fund domain (manual import first)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_nav (
    nav_id         text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    instrument_id  text        NOT NULL REFERENCES gptbridge_trading.instrument(instrument_id),
    nav            numeric     NOT NULL,
    nav_date       date        NOT NULL,
    currency       text,
    source         text        NOT NULL DEFAULT 'manual',
    imported_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (nav_id),
    UNIQUE (instrument_id, nav_date)
);

CREATE TABLE IF NOT EXISTS gptbridge_trading.fund_transaction (
    transaction_id text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    account_id     text        NOT NULL REFERENCES gptbridge_trading.account(account_id),
    instrument_id  text        NOT NULL REFERENCES gptbridge_trading.instrument(instrument_id),
    kind           text        NOT NULL,  -- subscription | redemption
    units          numeric     NOT NULL,
    amount         numeric     NOT NULL,
    currency       text,
    trade_date     date,
    source         text        NOT NULL DEFAULT 'manual',
    imported_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (transaction_id)
);
CREATE INDEX IF NOT EXISTS fund_transaction_idx
    ON gptbridge_trading.fund_transaction (instrument_id, trade_date DESC);

-- ============================================================================
-- strategy_version — governed strategy lineage
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_version (
    strategy_id    text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    version        int         NOT NULL,
    kind           text        NOT NULL,  -- signal-follow | ai-proposal | ...
    params         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    artifact       text,                   -- native model artifact ref
    status         text        NOT NULL DEFAULT 'active',
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, version)
);

-- ============================================================================
-- authorization_grant — explicit human authorization for LIVE
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.authorization_grant (
    grant_id       text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    scope          text        NOT NULL,           -- live-trading | strategy:<id>
    granted_by     text        NOT NULL,           -- human principal id
    strategy_ids   jsonb       NOT NULL DEFAULT '[]'::jsonb,
    granted_at     timestamptz NOT NULL DEFAULT now(),
    expires_at     timestamptz NOT NULL,
    revoked_at     timestamptz,
    payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (grant_id)
);
CREATE INDEX IF NOT EXISTS authorization_active_idx
    ON gptbridge_trading.authorization_grant (expires_at)
    WHERE revoked_at IS NULL;

-- ============================================================================
-- audit_event — append-only trading audit (no UPDATE/DELETE grants)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.audit_event (
    event_id       text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    type           text        NOT NULL,
    actor          text,
    payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id)
);
CREATE INDEX IF NOT EXISTS audit_event_type_idx
    ON gptbridge_trading.audit_event (type, at DESC);

-- ============================================================================
-- research_document — financial research docs indexed in Qdrant
--   (documents/pointers only — Qdrant never holds account/position data)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.research_document (
    document_id    text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    kind           text        NOT NULL,           -- report | filing | note | analysis
    title          text,
    source         text,
    qdrant_point   text,                            -- point id in star-financial-research-v1
    payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (document_id)
);

-- ============================================================================
-- Seed brokers — three declared adapters (fund platform intentionally
-- generic; NOT hardcoded to Cathay/Fubon)
-- ============================================================================
INSERT INTO gptbridge_trading.broker (broker_id, market, label)
VALUES
    ('CATHAY_SECURITIES', 'tw', '國泰綜合證券（台股）'),
    ('FUBON_SUBBROKERAGE', 'us', '富邦證券複委託（美股）'),
    ('MUTUAL_FUND_PROVIDER', 'fund', '共同基金平台（待接入）')
ON CONFLICT (broker_id) DO NOTHING;

-- ============================================================================
-- RLS + grants (008 pattern: module-scoped read/write)
-- ============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'instrument', 'broker', 'account', 'portfolio', 'position',
        'cash_balance', 'market_observation', 'trading_signal',
        'trade_proposal', 'risk_decision', 'order_request',
        'order_receipt', 'execution', 'portfolio_snapshot',
        'fund_nav', 'fund_transaction', 'strategy_version',
        'audit_event', 'authorization_grant', 'research_document'
    ]
    LOOP
        EXECUTE format(
            'ALTER TABLE gptbridge_trading.%I ENABLE ROW LEVEL SECURITY',
            t
        );
        EXECUTE format(
            'ALTER TABLE gptbridge_trading.%I FORCE ROW LEVEL SECURITY',
            t
        );
        EXECUTE format(
            'DROP POLICY IF EXISTS %I_read ON gptbridge_trading.%I',
            t, t
        );
        EXECUTE format(
            'CREATE POLICY %I_read ON gptbridge_trading.%I
             FOR SELECT USING (gptbridge_security.can_read(module_id))',
            t, t
        );
        EXECUTE format(
            'DROP POLICY IF EXISTS %I_write ON gptbridge_trading.%I',
            t, t
        );
        EXECUTE format(
            'CREATE POLICY %I_write ON gptbridge_trading.%I
             FOR ALL
             USING (gptbridge_security.can_write(module_id))
             WITH CHECK (
                 gptbridge_security.can_write(module_id)
                 AND module_id = current_setting(''app.current_module_id'', true)
             )',
            t, t
        );
        EXECUTE format(
            'REVOKE ALL ON gptbridge_trading.%I FROM PUBLIC', t
        );
        EXECUTE format(
            'GRANT SELECT ON gptbridge_trading.%I TO gptbridge_index_reader', t
        );
    END LOOP;

    -- audit_event is append-only: no UPDATE/DELETE anywhere.
    EXECUTE 'GRANT SELECT, INSERT ON gptbridge_trading.audit_event TO gptbridge_index_executor';

    -- mutable business tables
    FOREACH t IN ARRAY ARRAY[
        'instrument', 'broker', 'account', 'portfolio', 'position',
        'cash_balance', 'market_observation', 'trading_signal',
        'trade_proposal', 'risk_decision', 'order_request',
        'order_receipt', 'execution', 'portfolio_snapshot',
        'fund_nav', 'fund_transaction', 'strategy_version',
        'authorization_grant', 'research_document'
    ]
    LOOP
        EXECUTE format(
            'GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_trading.%I TO gptbridge_index_executor',
            t
        );
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA gptbridge_trading TO gptbridge_index_reader;
GRANT USAGE ON SCHEMA gptbridge_trading TO gptbridge_index_executor;

-- ============================================================================
-- Qdrant collection registration — financial research semantic index
-- ============================================================================
INSERT INTO gptbridge_index.qdrant_generation
    (collection_name, backend_generation, last_synced_at, stale)
VALUES ('star-financial-research-v1', 1, now(), false)
ON CONFLICT (collection_name) DO NOTHING;
