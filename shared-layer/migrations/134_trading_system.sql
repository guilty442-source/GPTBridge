-- 129_trading_system.sql
-- 星澄 AI 投資管理與自動操盤系統 — canonical trading schema.
--
-- Authoritative records for assets, holdings, transactions, orders,
-- fills, strategy signals, risk decisions, authorization grants and the
-- trading audit trail. The investment-mobile engine journal is a runtime
-- mirror; these tables are the canonical store owned by ai-assistant
-- (module_id = 'ai-assistant').
--
-- Codex basis:
--   A46/E22 — audit: mandatory-ledger (append-only audit_event).
--   A49/E35 + A44/E30 — RLS + module-scoped writes (008 pattern).
--   A8/E21  — Qdrant canonical semantic index (financial-research
--             collection registered in qdrant_generation).

CREATE SCHEMA IF NOT EXISTS gptbridge_trading;

-- ============================================================================
-- instrument — tradable instruments across the six domains
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.instrument (
    instrument_id  text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    market         text        NOT NULL,           -- tw | us | fund
    kind           text        NOT NULL,           -- stock | etf | fund
    symbol         text        NOT NULL,
    name           text,
    currency       text,
    broker_adapter text,                            -- cathay-tw | fubon-us-sub | fund-platform-*
    meta           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id),
    UNIQUE (market, symbol)
);

-- ============================================================================
-- holding — authoritative positions (derived from fill ledger + adjustments)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.holding (
    holding_id     text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    instrument_id  text        NOT NULL REFERENCES gptbridge_trading.instrument(instrument_id),
    quantity       numeric     NOT NULL DEFAULT 0,
    average_cost   numeric     NOT NULL DEFAULT 0,
    currency       text,
    meta           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (holding_id)
);
CREATE INDEX IF NOT EXISTS holding_instrument_idx
    ON gptbridge_trading.holding (instrument_id);

-- ============================================================================
-- ledger — authoritative transaction records
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.ledger (
    entry_id       text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    holding_id     text        REFERENCES gptbridge_trading.holding(holding_id),
    kind           text        NOT NULL,           -- buy | sell | dividend | fee | adjustment
    quantity       numeric,
    amount         numeric,
    currency       text,
    occurred_at    timestamptz NOT NULL DEFAULT now(),
    payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (entry_id)
);
CREATE INDEX IF NOT EXISTS ledger_time_idx
    ON gptbridge_trading.ledger (occurred_at DESC);

-- ============================================================================
-- signal — 星澄 candidate trade signals (advisory only)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.signal (
    signal_id      text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    market         text        NOT NULL,
    instrument     text        NOT NULL,
    side           text        NOT NULL,
    confidence     numeric,
    price          numeric,
    quantity       numeric,
    rationale      text,
    source         text        NOT NULL DEFAULT 'xingcheng',
    payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (signal_id)
);
CREATE INDEX IF NOT EXISTS signal_market_idx
    ON gptbridge_trading.signal (market, created_at DESC);

-- ============================================================================
-- trade_order + fill — OMS records (order is a reserved word)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trade_order (
    order_id       text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    signal_id      text        REFERENCES gptbridge_trading.signal(signal_id),
    strategy_id    text,
    market         text        NOT NULL,
    instrument     text        NOT NULL,
    side           text        NOT NULL,
    quantity       numeric     NOT NULL,
    price          numeric,
    status         text        NOT NULL DEFAULT 'created',
    mode           text        NOT NULL DEFAULT 'ANALYSIS',
    rejection      text,
    payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (order_id)
);
CREATE INDEX IF NOT EXISTS trade_order_status_idx
    ON gptbridge_trading.trade_order (status, created_at DESC);

CREATE TABLE IF NOT EXISTS gptbridge_trading.fill (
    fill_id        text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    order_id       text        NOT NULL REFERENCES gptbridge_trading.trade_order(order_id),
    quantity       numeric     NOT NULL,
    price          numeric     NOT NULL,
    simulated      boolean     NOT NULL DEFAULT true,
    executed_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (fill_id)
);
CREATE INDEX IF NOT EXISTS fill_order_idx
    ON gptbridge_trading.fill (order_id);

-- ============================================================================
-- risk_decision — every risk evaluation is recorded (approved or not)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.risk_decision (
    decision_id    text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    order_id       text,
    approved       boolean     NOT NULL,
    reasons        jsonb       NOT NULL DEFAULT '[]'::jsonb,
    limits_checked jsonb       NOT NULL DEFAULT '[]'::jsonb,
    backend        text        NOT NULL DEFAULT 'python',
    evaluated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (decision_id)
);
CREATE INDEX IF NOT EXISTS risk_decision_order_idx
    ON gptbridge_trading.risk_decision (order_id);

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
-- RLS + grants (008 pattern: module-scoped read/write)
-- ============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'instrument', 'holding', 'ledger', 'signal', 'trade_order',
        'fill', 'risk_decision', 'authorization_grant', 'audit_event',
        'research_document'
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
        'instrument', 'holding', 'ledger', 'signal', 'trade_order',
        'fill', 'risk_decision', 'authorization_grant', 'research_document'
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
