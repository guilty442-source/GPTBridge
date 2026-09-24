-- ============================================================================
-- 140_live_trading.sql — formal trading core (LIVE path).
--
-- Schema: gptbridge_trading (trading engines; business owner ai-assistant).
-- Isolation: every live table carries ``simulated boolean NOT NULL DEFAULT
-- false CHECK (NOT simulated)`` — a PAPER/simulated row can NEVER be
-- written as a live record. Simulation tables live in 139_simulation_trading.
-- LIVE dispatch itself stays phase-locked; these tables record intents,
-- decisions, submissions, reports and evidence.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS gptbridge_trading;

-- ============================================================================
-- trading_authorization — scoped, expiring grants (issued by formal
-- authority only; AI can never mint its own)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trading_authorization (
    authorization_id text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    account_id       text NOT NULL,
    user_id          text NOT NULL DEFAULT '',
    market           text NOT NULL DEFAULT '',
    broker_id        text NOT NULL DEFAULT '',
    instrument_ids   jsonb NOT NULL DEFAULT '[]',
    strategy_ids     jsonb NOT NULL DEFAULT '[]',
    modes            jsonb NOT NULL DEFAULT '["LIVE"]',
    sides            jsonb NOT NULL DEFAULT '["buy","sell"]',
    max_order_notional  numeric NOT NULL DEFAULT 0,
    max_daily_notional  numeric NOT NULL DEFAULT 0,
    currency         text NOT NULL DEFAULT '',
    issued_by        text NOT NULL,
    issued_at        timestamptz NOT NULL DEFAULT now(),
    expires_at       timestamptz NOT NULL,
    status           text NOT NULL DEFAULT 'ACTIVE'
                     CHECK (status IN ('ACTIVE','REVOKED','EXPIRED')),
    scope_version    integer NOT NULL DEFAULT 1,
    simulated        boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (authorization_id)
);

-- ============================================================================
-- trading_risk_rule — governed limit versions (AI can propose, never apply)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trading_risk_rule (
    rule_id          text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    rule_version     text NOT NULL,
    scope            text NOT NULL DEFAULT 'global', -- global|account|strategy
    scope_key        text NOT NULL DEFAULT '',
    limits           jsonb NOT NULL,
    effective_from   timestamptz NOT NULL DEFAULT now(),
    supersedes       text NOT NULL DEFAULT '',
    status           text NOT NULL DEFAULT 'ACTIVE'
                     CHECK (status IN ('ACTIVE','SUPERSEDED','REVOKED')),
    simulated        boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (rule_id)
);

-- ============================================================================
-- trading_risk_decision — every evaluation is evidence
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trading_risk_decision (
    risk_decision_id text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    proposal_id      text NOT NULL,
    account_id       text NOT NULL,
    strategy_id      text NOT NULL DEFAULT '',
    rule_version     text NOT NULL,
    decision         text NOT NULL CHECK (decision IN
                     ('ALLOW','DENY','INCOMPLETE_EVIDENCE')),
    reason_codes     jsonb NOT NULL DEFAULT '[]',
    evaluated_at     timestamptz NOT NULL DEFAULT now(),
    simulated        boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (risk_decision_id)
);
CREATE INDEX IF NOT EXISTS trading_risk_decision_proposal_idx
    ON gptbridge_trading.trading_risk_decision (proposal_id);

-- ============================================================================
-- trading_order — formal order intents; state via events below
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trading_order (
    internal_order_id text NOT NULL,
    module_id         text NOT NULL DEFAULT 'ai-assistant',
    client_order_key  text NOT NULL,
    proposal_id       text NOT NULL,
    account_id        text NOT NULL,
    broker_id         text NOT NULL,
    instrument_id     text NOT NULL,
    side              text NOT NULL,
    order_type        text NOT NULL,
    quantity          numeric NOT NULL,
    limit_price       numeric,
    currency          text NOT NULL DEFAULT '',
    market            text NOT NULL DEFAULT '',
    strategy_id       text NOT NULL DEFAULT '',
    strategy_version  text NOT NULL DEFAULT '',
    state             text NOT NULL DEFAULT 'CREATED',
    filled_quantity   numeric NOT NULL DEFAULT 0,
    avg_fill_price    numeric,
    authorization_id  text NOT NULL DEFAULT '',
    risk_decision_id  text NOT NULL DEFAULT '',
    broker_order_id   text NOT NULL DEFAULT '',
    expires_at        timestamptz,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    simulated         boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (internal_order_id),
    UNIQUE (account_id, client_order_key)
);
CREATE INDEX IF NOT EXISTS trading_order_state_idx
    ON gptbridge_trading.trading_order (account_id, state);

-- ============================================================================
-- trading_order_event — append-only state transitions
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trading_order_event (
    event_id         text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    order_id         text NOT NULL
                     REFERENCES gptbridge_trading.trading_order(internal_order_id),
    from_state       text NOT NULL DEFAULT '',
    to_state         text NOT NULL,
    reason           text NOT NULL DEFAULT '',
    at               timestamptz NOT NULL DEFAULT now(),
    simulated        boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (event_id)
);
CREATE INDEX IF NOT EXISTS trading_order_event_order_idx
    ON gptbridge_trading.trading_order_event (order_id, at);

-- ============================================================================
-- trading_execution — broker-reported fills only (never fabricated)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trading_execution (
    execution_id        text NOT NULL,
    module_id           text NOT NULL DEFAULT 'ai-assistant',
    order_id            text NOT NULL
                        REFERENCES gptbridge_trading.trading_order(internal_order_id),
    account_id          text NOT NULL,
    instrument_id       text NOT NULL,
    side                text NOT NULL,
    quantity            numeric NOT NULL,
    price               numeric NOT NULL,
    broker_execution_id text NOT NULL DEFAULT '',
    commission          numeric NOT NULL DEFAULT 0,
    fees                jsonb NOT NULL DEFAULT '{}',
    currency            text NOT NULL DEFAULT '',
    reported_at         timestamptz NOT NULL DEFAULT now(),
    simulated           boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (execution_id)
);
CREATE INDEX IF NOT EXISTS trading_execution_order_idx
    ON gptbridge_trading.trading_execution (order_id, reported_at);

-- ============================================================================
-- trading_submission — every broker-contact attempt (ack|reject|
-- timeout|unknown|blocked) — SUBMISSION_UNKNOWN evidence lives here
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trading_submission (
    submission_id    text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    order_id         text NOT NULL
                     REFERENCES gptbridge_trading.trading_order(internal_order_id),
    client_order_key text NOT NULL,
    account_id       text NOT NULL,
    broker_id        text NOT NULL,
    outcome          text NOT NULL CHECK (outcome IN
                     ('ack','reject','timeout','unknown','blocked')),
    broker_order_id  text NOT NULL DEFAULT '',
    detail           jsonb NOT NULL DEFAULT '{}',
    submitted_at     timestamptz NOT NULL DEFAULT now(),
    simulated        boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (submission_id)
);
CREATE INDEX IF NOT EXISTS trading_submission_key_idx
    ON gptbridge_trading.trading_submission (client_order_key);

-- ============================================================================
-- trading_account_snapshot — broker-reported truth (external fact source)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trading_account_snapshot (
    snapshot_id      text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    account_id       text NOT NULL,
    source           text NOT NULL DEFAULT 'broker',
    balances         jsonb NOT NULL DEFAULT '{}',
    positions        jsonb NOT NULL DEFAULT '[]',
    open_orders      jsonb NOT NULL DEFAULT '[]',
    fetched_at       timestamptz NOT NULL DEFAULT now(),
    simulated        boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (snapshot_id)
);
CREATE INDEX IF NOT EXISTS trading_account_snapshot_idx
    ON gptbridge_trading.trading_account_snapshot (account_id, fetched_at DESC);

-- ============================================================================
-- trading_reconciliation — local-vs-broker comparison results
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trading_reconciliation (
    report_id        text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    account_id       text NOT NULL,
    result           text NOT NULL CHECK (result IN
                     ('MATCHED','MISMATCHED','INCOMPLETE','UNAVAILABLE')),
    checked          jsonb NOT NULL DEFAULT '[]',
    mismatches       jsonb NOT NULL DEFAULT '[]',
    local_revision   text NOT NULL DEFAULT '',
    remote_revision  text NOT NULL DEFAULT '',
    reconciled_at    timestamptz NOT NULL DEFAULT now(),
    simulated        boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (report_id)
);
CREATE INDEX IF NOT EXISTS trading_reconciliation_idx
    ON gptbridge_trading.trading_reconciliation (account_id, reconciled_at DESC);

-- ============================================================================
-- trading_emergency_event — engage/release audit (release = authorized
-- actor only; cancel-open and liquidate are separate operations)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trading_emergency_event (
    event_id         text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    scope            text NOT NULL CHECK (scope IN
                     ('ALL','MARKET','ACCOUNT','STRATEGY')),
    scope_key        text NOT NULL DEFAULT '',
    action           text NOT NULL CHECK (action IN
                     ('stop_new_orders','cancel_open','liquidate','release')),
    actor            text NOT NULL,
    reason           text NOT NULL DEFAULT '',
    at               timestamptz NOT NULL DEFAULT now(),
    simulated        boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (event_id)
);

-- ============================================================================
-- trading_audit_event — correlated answer-every-question journal
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.trading_audit_event (
    event_id         text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    correlation_id   text NOT NULL DEFAULT '',
    event_type       text NOT NULL,
    account_id       text NOT NULL DEFAULT '',
    proposal_id      text NOT NULL DEFAULT '',
    order_id         text NOT NULL DEFAULT '',
    strategy_version text NOT NULL DEFAULT '',
    authorization_id text NOT NULL DEFAULT '',
    risk_decision_id text NOT NULL DEFAULT '',
    result           text NOT NULL DEFAULT '',
    detail           jsonb NOT NULL DEFAULT '{}',  -- sensitive keys masked
    at               timestamptz NOT NULL DEFAULT now(),
    simulated        boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (event_id)
);
CREATE INDEX IF NOT EXISTS trading_audit_event_corr_idx
    ON gptbridge_trading.trading_audit_event (correlation_id, at);

-- ============================================================================
-- broker_capability — declared capability matrix per broker/function
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.broker_capability (
    capability_id    text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    broker_id        text NOT NULL,
    function         text NOT NULL CHECK (function IN
                     ('connect','disconnect','get_account','get_balance',
                      'get_positions','get_orders','get_executions',
                      'place_order','cancel_order','modify_order')),
    status           text NOT NULL CHECK (status IN
                     ('SUPPORTED','UNSUPPORTED','UNKNOWN')),
    verified_by      text NOT NULL DEFAULT '',
    verified_at      timestamptz,
    evidence         jsonb NOT NULL DEFAULT '{}',
    simulated        boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (capability_id),
    UNIQUE (broker_id, function)
);

-- ============================================================================
-- broker_connection_state — last known connection/disconnect evidence
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.broker_connection_state (
    state_id         text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    broker_id        text NOT NULL,
    connected        boolean NOT NULL DEFAULT false,
    detail           text NOT NULL DEFAULT '',
    at               timestamptz NOT NULL DEFAULT now(),
    simulated        boolean NOT NULL DEFAULT false CHECK (NOT simulated),
    PRIMARY KEY (state_id)
);
CREATE INDEX IF NOT EXISTS broker_connection_state_idx
    ON gptbridge_trading.broker_connection_state (broker_id, at DESC);

-- ============================================================================
-- RLS + grants (008/134–139 pattern)
-- ============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'trading_authorization', 'trading_risk_rule',
        'trading_risk_decision', 'trading_order', 'trading_order_event',
        'trading_execution', 'trading_submission',
        'trading_account_snapshot', 'trading_reconciliation',
        'trading_emergency_event', 'trading_audit_event',
        'broker_capability', 'broker_connection_state'
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
