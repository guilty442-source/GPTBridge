-- ============================================================================
-- 143_monitoring_center.sql — 星澄 AI investment monitoring center.
--
-- Schema: gptbridge_trading (business owner ai-assistant).
--
-- Authority boundaries (no duplicates):
--   * Recommendations, recommendation versions/outcomes and analysis
--     schedules remain in 137_ai_intelligence.sql
--     (investment_recommendation / recommendation_version /
--      recommendation_outcome / analysis_schedule) — reused, not rebuilt.
--   * Account/holding/cash/transaction authority stays in
--     141_offline_broker.sql; asset projections in 142_asset_management.sql.
--   * Market candles stay in the market schema of earlier migrations.
--
-- This migration adds only monitoring-specific state: registered
-- monitors, normalized monitoring events, user-set alert rules, fired
-- alerts, deduplicated notifications, reallocation proposals and
-- versioned investment reports.
--
-- Everything here is observational/advisory: CHECKs forbid
-- broker_confirmed rows and AI actors may never set rules or proposals
-- (set_by / proposed_by cannot be an AI identity).
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS gptbridge_trading;

-- ============================================================================
-- investment_monitor — registered monitor instances (tw / us / fund /
-- portfolio / risk / cross-market). Configuration only; monitors emit
-- events, they never trade.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.investment_monitor (
    monitor_id       text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    monitor_kind     text NOT NULL,      -- tw|us|fund|portfolio|risk|cross_market|schedule
    market           text,
    enabled          boolean NOT NULL DEFAULT true,
    config           jsonb NOT NULL DEFAULT '{}',
    last_scan_at     timestamptz,
    last_event_at    timestamptz,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (monitor_id)
);

-- ============================================================================
-- monitoring_event — normalized MonitoringEvent. Stable identity via
-- dedupe_key so repeated identical detections update instead of
-- flooding. source_timestamp preserves the data vintage separately
-- from detection time.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.monitoring_event (
    event_id         text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    dedupe_key       text NOT NULL,
    event_type       text NOT NULL,
    instrument_id    text,
    account_id       text,
    market           text,
    severity         text NOT NULL
        CHECK (severity IN ('INFO','NOTICE','WARNING','CRITICAL')),
    title            text NOT NULL DEFAULT '',
    detail           jsonb NOT NULL DEFAULT '{}',
    evidence_refs    jsonb NOT NULL DEFAULT '[]',
    detected_at      timestamptz NOT NULL,
    source_timestamp timestamptz,
    status           text NOT NULL DEFAULT 'OPEN'
        CHECK (status IN ('OPEN','RESOLVED','ARCHIVED')),
    occurrences      integer NOT NULL DEFAULT 1,
    broker_confirmed boolean NOT NULL DEFAULT false
        CHECK (broker_confirmed = false),
    created_at       timestamptz NOT NULL DEFAULT now(),
    resolved_at      timestamptz,
    PRIMARY KEY (event_id),
    UNIQUE (dedupe_key)
);

-- ============================================================================
-- monitoring_rule — user/strategy-defined alert conditions. set_by may
-- never be an AI identity: 星澄 proposes, humans own thresholds.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.monitoring_rule (
    rule_id          text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    kind             text NOT NULL,      -- price_above|price_below|ma_cross|
                                         -- volume_spike|volatility_spike|
                                         -- drawdown|allocation_drift|
                                         -- nav_change|fund_notice|...
    instrument_id    text,
    market           text,
    threshold        jsonb NOT NULL DEFAULT '{}',
    severity         text NOT NULL DEFAULT 'WARNING'
        CHECK (severity IN ('INFO','NOTICE','WARNING','CRITICAL')),
    cooldown_seconds integer NOT NULL DEFAULT 3600,
    enabled          boolean NOT NULL DEFAULT true,
    set_by           text NOT NULL DEFAULT 'user'
        CHECK (set_by NOT IN ('ai','xingcheng','model','assistant')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (rule_id)
);

-- ============================================================================
-- monitoring_alert — a fired rule evaluation; traceable to its rule and
-- the triggering event.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.monitoring_alert (
    alert_id         text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    rule_id          text NOT NULL
        REFERENCES gptbridge_trading.monitoring_rule(rule_id),
    event_id         text
        REFERENCES gptbridge_trading.monitoring_event(event_id),
    severity         text NOT NULL
        CHECK (severity IN ('INFO','NOTICE','WARNING','CRITICAL')),
    fired_at         timestamptz NOT NULL DEFAULT now(),
    status           text NOT NULL DEFAULT 'FIRED'
        CHECK (status IN ('FIRED','ACKNOWLEDGED','RESOLVED','SUPPRESSED')),
    detail           jsonb NOT NULL DEFAULT '{}',
    broker_confirmed boolean NOT NULL DEFAULT false
        CHECK (broker_confirmed = false),
    PRIMARY KEY (alert_id)
);

-- ============================================================================
-- monitoring_notification — deduplicated, cooldown-gated notification
-- history. dedupe_key + occurrences implements merge/update semantics;
-- a notification can never execute a trade.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.monitoring_notification (
    notification_id  text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    dedupe_key       text NOT NULL,
    event_id         text
        REFERENCES gptbridge_trading.monitoring_event(event_id),
    alert_id         text
        REFERENCES gptbridge_trading.monitoring_alert(alert_id),
    channel          text NOT NULL DEFAULT 'in_app',
    severity         text NOT NULL
        CHECK (severity IN ('INFO','NOTICE','WARNING','CRITICAL')),
    title            text NOT NULL DEFAULT '',
    body             text NOT NULL DEFAULT '',
    payload          jsonb NOT NULL DEFAULT '{}',
    occurrences      integer NOT NULL DEFAULT 1,
    delivered_at     timestamptz NOT NULL DEFAULT now(),
    read_at          timestamptz,
    PRIMARY KEY (notification_id),
    UNIQUE (dedupe_key)
);

-- ============================================================================
-- portfolio_reallocation_proposal — candidate reallocation. AI may
-- propose (proposed_by='model' allowed) but can never mark a proposal
-- accepted/applied; the CHECK also rejects AI-owned target authority.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.portfolio_reallocation_proposal (
    proposal_id      text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    dimension        text NOT NULL DEFAULT 'market',
    before_allocation jsonb NOT NULL DEFAULT '{}',
    after_allocation  jsonb NOT NULL DEFAULT '{}',
    estimated_costs  jsonb NOT NULL DEFAULT '{}',
    risk_change      jsonb NOT NULL DEFAULT '{}',
    data_sources     jsonb NOT NULL DEFAULT '[]',
    assumptions      jsonb NOT NULL DEFAULT '[]',
    proposed_by      text NOT NULL DEFAULT 'engine'
        CHECK (proposed_by NOT IN ('ai','xingcheng','assistant')),
    status           text NOT NULL DEFAULT 'PROPOSED'
        CHECK (status IN ('PROPOSED','REVIEWED','ACCEPTED','REJECTED',
                          'EXPIRED','APPLIED')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (proposal_id)
);

-- ============================================================================
-- investment_report + investment_report_version — daily/weekly/monthly
-- reports, append-only versions (supersedes chain), explicit data-gap
-- section so missing scope is never filled with fabricated numbers.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.investment_report (
    report_id        text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    report_type      text NOT NULL
        CHECK (report_type IN ('daily','weekly','monthly','adhoc')),
    period_start     date,
    period_end       date,
    generated_at     timestamptz NOT NULL DEFAULT now(),
    model_id         text,
    model_version    text,
    data_gaps        jsonb NOT NULL DEFAULT '[]',
    content          jsonb NOT NULL DEFAULT '{}',
    latest_version   integer NOT NULL DEFAULT 1,
    PRIMARY KEY (report_id)
);

CREATE TABLE IF NOT EXISTS gptbridge_trading.investment_report_version (
    version_id       text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    report_id        text NOT NULL
        REFERENCES gptbridge_trading.investment_report(report_id),
    version          integer NOT NULL,
    content          jsonb NOT NULL DEFAULT '{}',
    data_gaps        jsonb NOT NULL DEFAULT '[]',
    supersedes       text,
    generated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (version_id),
    UNIQUE (report_id, version)
);

-- ============================================================================
-- RLS + ownership (identical convention to 140/141/142)
-- ============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'investment_monitor', 'monitoring_event', 'monitoring_rule',
        'monitoring_alert', 'monitoring_notification',
        'portfolio_reallocation_proposal',
        'investment_report', 'investment_report_version'
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
