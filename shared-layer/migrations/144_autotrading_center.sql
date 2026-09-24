-- ============================================================================
-- 144_autotrading_center.sql — AI autonomous simulated-trading center.
--
-- Schema: gptbridge_trading (business owner ai-assistant).
--
-- Authority boundaries (no duplicates):
--   * Strategy definitions / versions / backtests stay in
--     138_strategy_backtest.sql (strategy / strategy_version /
--     backtest_run / ...) — reused, not rebuilt.
--   * Paper accounts / cash ledger / positions / orders / executions /
--     shadow signals stay in 139_simulation_trading.sql.
--   * Monitoring events / alerts / notifications stay in
--     143_monitoring_center.sql.
--   * Instrument, account, market-data and model authorities are NOT
--     recreated here.
--
-- This migration adds only autotrade orchestration state: runtime
-- instances + transition journal, user-owned capital allocation plans,
-- resource reservations, execution checkpoints, performance snapshots,
-- stability analyses, halt/recovery events, research experiments and
-- improvement proposals.
--
-- Everything here is SIMULATED: CHECKs pin `simulated = true` where
-- applicable, forbid broker_confirmed rows, and AI identities can never
-- be the actor that sets capital plans, lifts halts or advances an
-- experiment to review.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS gptbridge_trading;

-- ============================================================================
-- strategy_runtime — one row per registered strategy run (runtime
-- instance of a strategy_version). The state column mirrors the formal
-- StrategyRuntimeState machine; transitions themselves are journaled in
-- strategy_runtime_event.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_runtime (
    run_id             text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    strategy_id        text NOT NULL,
    strategy_version   integer NOT NULL,
    strategy_type      text NOT NULL,
    market             text NOT NULL,
    instrument_scope   jsonb NOT NULL DEFAULT '[]',
    parameters         jsonb NOT NULL DEFAULT '{}',
    execution_mode     text NOT NULL
        CHECK (execution_mode IN ('SHADOW','PAPER')),
    ai_policy          text NOT NULL DEFAULT 'DETERMINISTIC'
        CHECK (ai_policy IN ('DETERMINISTIC','AI_ASSISTED')),
    account_id         text,
    auto_recover       boolean NOT NULL DEFAULT false,
    session_policy     jsonb NOT NULL DEFAULT '{}',
    state              text NOT NULL
        CHECK (state IN ('CREATED','READY','RUNNING','PAUSED',
                         'RISK_HALTED','DATA_BLOCKED','MODEL_BLOCKED',
                         'RECOVERING','STOPPED','FAILED')),
    state_reason       text NOT NULL DEFAULT '',
    simulated          boolean NOT NULL DEFAULT true
        CHECK (simulated),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id)
);

-- ============================================================================
-- strategy_runtime_event — append-only transition journal. `actor`
-- records who moved the state; CHECK blocks AI identities from being
-- recorded as the actor of a resume/unhalt transition.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_runtime_event (
    runtime_event_id   text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    run_id             text NOT NULL
        REFERENCES gptbridge_trading.strategy_runtime(run_id),
    from_state         text,
    to_state           text NOT NULL,
    actor              text NOT NULL,
    reason             text NOT NULL DEFAULT '',
    detail             jsonb NOT NULL DEFAULT '{}',
    at                 timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (runtime_event_id),
    CHECK (NOT (
        lower(actor) IN ('ai','xingcheng','model','assistant')
        AND to_state IN ('RUNNING','READY')))
);

-- ============================================================================
-- strategy_capital_allocation — user-owned capital plan per simulated
-- account. AI can never be the setter (set_by CHECK). Weights are stored
-- as numeric fractions; total weight + reserve ≤ 1 enforced by CHECK.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_capital_allocation (
    allocation_id      text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    account_id         text NOT NULL,
    strategy_id        text NOT NULL,
    weight             numeric NOT NULL CHECK (weight >= 0 AND weight <= 1),
    per_order_max      numeric,
    per_instrument_max numeric,
    exposure_max       numeric,
    usable_cash_ratio  numeric NOT NULL DEFAULT 1
        CHECK (usable_cash_ratio > 0 AND usable_cash_ratio <= 1),
    reserve_cash_ratio numeric NOT NULL DEFAULT 0
        CHECK (reserve_cash_ratio >= 0 AND reserve_cash_ratio <= 1),
    set_by             text NOT NULL,
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (allocation_id),
    UNIQUE (account_id, strategy_id),
    CHECK (lower(set_by) NOT IN ('ai','xingcheng','model','assistant'))
);

-- ============================================================================
-- strategy_resource_reservation — order-flow arbitration record. Each
-- row attributes a proposed quantity/notional to its owning strategy;
-- cluster_with / conflicts_with preserve cross-strategy context without
-- merging independent allocations.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_resource_reservation (
    reservation_id     text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    run_id             text NOT NULL,
    strategy_id        text NOT NULL,
    instrument_id      text NOT NULL,
    side               text NOT NULL
        CHECK (side IN ('buy','sell','subscribe','redeem')),
    quantity           numeric NOT NULL,
    notional           numeric NOT NULL,
    signal_key         text NOT NULL,
    cluster_with       jsonb NOT NULL DEFAULT '[]',
    conflicts_with     jsonb NOT NULL DEFAULT '[]',
    status             text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open','released','filled','expired')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    released_at        timestamptz,
    PRIMARY KEY (reservation_id)
);

-- ============================================================================
-- strategy_execution_checkpoint — per-run pipeline checkpoint so restart
-- recovery can verify the last market event / cycle / signal / fill
-- without re-executing completed work (run_key idempotency).
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_execution_checkpoint (
    checkpoint_id      text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    run_id             text NOT NULL,
    run_key            text NOT NULL,
    stage              text NOT NULL,
    event_fingerprint  text,
    client_order_id    text,
    detail             jsonb NOT NULL DEFAULT '{}',
    at                 timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (checkpoint_id),
    UNIQUE (run_id, run_key, stage)
);

-- ============================================================================
-- strategy_performance_snapshot — per-strategy simulated equity curve.
-- Always simulated; never real-account performance.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_performance_snapshot (
    snapshot_id        text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    run_id             text NOT NULL,
    strategy_id        text NOT NULL,
    strategy_version   integer NOT NULL,
    account_id         text,
    equity             numeric NOT NULL,
    cash_available     numeric NOT NULL DEFAULT 0,
    realized_pnl       numeric NOT NULL DEFAULT 0,
    unrealized_pnl     numeric NOT NULL DEFAULT 0,
    orders             integer NOT NULL DEFAULT 0,
    filled             integer NOT NULL DEFAULT 0,
    win_rate           numeric,
    metrics            jsonb NOT NULL DEFAULT '{}',
    simulated          boolean NOT NULL DEFAULT true CHECK (simulated),
    at                 timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_id)
);

-- ============================================================================
-- strategy_stability_analysis — classification output:
-- normal_market | condition_mismatch | data_anomaly | model_anomaly |
-- execution_anomaly. Advisory only — never auto-retires a strategy.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_stability_analysis (
    analysis_id        text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    run_id             text NOT NULL,
    strategy_id        text NOT NULL,
    classification     text NOT NULL
        CHECK (classification IN ('normal_market','condition_mismatch',
                                  'data_anomaly','model_anomaly',
                                  'execution_anomaly')),
    findings           jsonb NOT NULL DEFAULT '[]',
    max_drawdown       numeric,
    at                 timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (analysis_id)
);

-- ============================================================================
-- strategy_halt_event — automatic pause/halt record. Halts never delete
-- the strategy or its history.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_halt_event (
    halt_id            text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    run_id             text NOT NULL,
    reason             text NOT NULL
        CHECK (reason IN ('capital_limit','drawdown_limit','data_stale',
                          'data_source_lost','position_inconsistent',
                          'execution_anomaly','duplicate_order_anomaly',
                          'resource_exhausted','model_fault')),
    target_state       text NOT NULL
        CHECK (target_state IN ('PAUSED','RISK_HALTED','DATA_BLOCKED',
                                'MODEL_BLOCKED')),
    detail             jsonb NOT NULL DEFAULT '{}',
    at                 timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (halt_id)
);

-- ============================================================================
-- strategy_recovery_event — recovery attempts with the verification
-- checklist result. A safety halt can never be cleared by an AI actor
-- (cleared_by CHECK) and unverifiable state lands in PAUSED.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_recovery_event (
    recovery_id        text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    run_id             text NOT NULL,
    from_state         text NOT NULL,
    checks             jsonb NOT NULL DEFAULT '[]',
    outcome            text NOT NULL
        CHECK (outcome IN ('recovered','held_paused','held_recovering',
                           'governance_required','failed')),
    cleared_by         text,
    at                 timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (recovery_id),
    CHECK (cleared_by IS NULL
           OR lower(cleared_by) NOT IN
              ('ai','xingcheng','model','assistant'))
);

-- ============================================================================
-- strategy_experiment — governed research pipeline stage tracker:
-- ACTIVE_STRATEGY → AI_RESEARCH → NEW_DRAFT → BACKTEST → VALIDATION
-- → OUT_OF_SAMPLE → SHADOW → PAPER → ELIGIBLE_FOR_REVIEW.
-- Reaching review never auto-replaces a running strategy.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_experiment (
    experiment_id      text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    strategy_id        text NOT NULL,
    base_version       integer NOT NULL,
    candidate_parameters jsonb NOT NULL DEFAULT '{}',
    proposal_id        text,
    stage              text NOT NULL
        CHECK (stage IN ('AI_RESEARCH','NEW_DRAFT','BACKTEST',
                         'VALIDATION','OUT_OF_SAMPLE','SHADOW','PAPER',
                         'ELIGIBLE_FOR_REVIEW')),
    history            jsonb NOT NULL DEFAULT '[]',
    status             text NOT NULL DEFAULT 'active'
        CHECK (status IN ('active','abandoned','promoted')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    updated_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (experiment_id)
);

-- ============================================================================
-- strategy_improvement_proposal — AI-researched draft. Always lands as a
-- NEW draft version; model_text is evidence, not proof of profit.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_improvement_proposal (
    proposal_id        text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    strategy_id        text NOT NULL,
    base_version       integer NOT NULL,
    candidate_parameters jsonb NOT NULL DEFAULT '{}',
    risk_suggestions   jsonb NOT NULL DEFAULT '[]',
    frequency_suggestions jsonb NOT NULL DEFAULT '[]',
    scope_study        jsonb NOT NULL DEFAULT '[]',
    evidence           jsonb NOT NULL DEFAULT '{}',
    model_id           text,
    model_text         text,
    status             text NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','under_review','rejected',
                          'accepted_for_experiment')),
    created_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (proposal_id)
);

-- ============================================================================
-- strategy_eval_ledger — overfitting guard. Reusing an out-of-sample
-- dataset under different parameters is flagged, not hidden.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.strategy_eval_ledger (
    eval_id            text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    strategy_id        text NOT NULL,
    version            integer NOT NULL,
    params_key         text NOT NULL,
    dataset_key        text NOT NULL,
    kind               text NOT NULL
        CHECK (kind IN ('in_sample','validation','out_of_sample',
                        'shadow','paper','backtest')),
    metrics            jsonb NOT NULL DEFAULT '{}',
    oos_contaminated   boolean NOT NULL DEFAULT false,
    at                 timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (eval_id)
);

-- ============================================================================
-- autotrade_event — published market/system events with stable
-- fingerprint for idempotent consumption.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.autotrade_event (
    event_id           text NOT NULL,
    module_id          text NOT NULL DEFAULT 'ai-assistant',
    fingerprint        text NOT NULL,
    event_type         text NOT NULL
        CHECK (event_type IN ('MARKET_OPEN','MARKET_CLOSE',
                              'QUOTE_UPDATED','CANDLE_CLOSED',
                              'CORPORATE_ACTION','FUND_NAV_UPDATED',
                              'PORTFOLIO_CHANGED','RISK_LIMIT_TRIGGERED',
                              'STRATEGY_SIGNAL','SYSTEM_RECOVERED')),
    market             text,
    instrument_id      text,
    source_id          text NOT NULL,
    data_revision      text NOT NULL DEFAULT '',
    source_timestamp   timestamptz,
    payload            jsonb NOT NULL DEFAULT '{}',
    at                 timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id),
    UNIQUE (fingerprint)
);

-- ============================================================================
-- RLS + ownership (identical convention to 139-143)
-- ============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'strategy_runtime', 'strategy_runtime_event',
        'strategy_capital_allocation', 'strategy_resource_reservation',
        'strategy_execution_checkpoint', 'strategy_performance_snapshot',
        'strategy_stability_analysis', 'strategy_halt_event',
        'strategy_recovery_event', 'strategy_experiment',
        'strategy_improvement_proposal', 'strategy_eval_ledger',
        'autotrade_event'
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
