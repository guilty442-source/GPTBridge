-- ============================================================================
-- 142_asset_management.sql — unified offline asset-management center.
--
-- Schema: gptbridge_trading (business owner ai-assistant).
--
-- Authority boundaries (no duplicates):
--   * Accounts/holdings/cash/transactions/dividends remain in the
--     offline_account*/investment_import* tables of 141_offline_broker.sql.
--   * Paper books stay in 139_simulation_trading.sql (simulated=true);
--     formal LIVE in 140_live_trading.sql (simulated=false).
--
-- This migration adds only the asset-center projections the offline
-- schema lacks: stable account registry, valuations, versioned
-- snapshots, allocation targets, look-through exposure baskets and
-- import reconciliation reports.
--
-- Every row is manual/imported — CHECKs forbid broker_confirmed and
-- simulated records alike; neither may masquerade in this schema.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS gptbridge_trading;

-- ============================================================================
-- asset_account_registry — stable account ids (CATHAY_TW, FUBON_US,
-- MUTUAL_FUND, CASH_TWD, CASH_USD, future platforms). The display label
-- is mutable; account_id is the only identity.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.asset_account_registry (
    account_id       text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    label            text NOT NULL DEFAULT '',
    kind             text NOT NULL
                     CHECK (kind IN ('brokerage','sub-brokerage',
                                     'fund','cash','platform')),
    broker_id        text NOT NULL DEFAULT '',
    market           text NOT NULL DEFAULT '',
    currency         text NOT NULL DEFAULT '',
    offline_account_id text,                       -- link to 141 books
    broker_confirmed boolean NOT NULL DEFAULT false
                     CHECK (NOT broker_confirmed),
    simulated        boolean NOT NULL DEFAULT false
                     CHECK (NOT simulated),
    created_at       timestamptz NOT NULL DEFAULT now(),
    renamed_at       timestamptz,
    PRIMARY KEY (account_id)
);

-- ============================================================================
-- asset_valuation — point-in-time valuation output. Per-source
-- valuation timestamps are recorded honestly; a stale aggregate is
-- reference_only, never a precise single-instant figure.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.asset_valuation (
    valuation_id     text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    display_currency text NOT NULL CHECK (display_currency IN ('TWD','USD')),
    total_assets     numeric NOT NULL,
    total_cash       numeric NOT NULL DEFAULT 0,
    total_market_value numeric NOT NULL DEFAULT 0,
    per_account      jsonb NOT NULL DEFAULT '{}',
    per_market       jsonb NOT NULL DEFAULT '{}',
    per_currency     jsonb NOT NULL DEFAULT '{}',
    valuation_times  jsonb NOT NULL DEFAULT '{}',  -- market → epoch
    stale_data       boolean NOT NULL DEFAULT false,
    reference_only   boolean NOT NULL DEFAULT false,
    data_version     text NOT NULL DEFAULT '',
    valued_at        timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (valuation_id)
);

-- ============================================================================
-- asset_portfolio_snapshot — immutable daily/weekly/monthly snapshots.
-- Re-imported history creates a NEW version (version++, supersedes);
-- history is never overwritten in place.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.asset_portfolio_snapshot (
    snapshot_id      text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    period           text NOT NULL
                     CHECK (period IN ('daily','weekly','monthly')),
    day_epoch        bigint NOT NULL,
    version          integer NOT NULL CHECK (version >= 1),
    supersedes       integer,
    total_assets     numeric,
    per_account      jsonb NOT NULL DEFAULT '{}',
    positions        jsonb NOT NULL DEFAULT '[]',
    cash_detail      jsonb NOT NULL DEFAULT '{}',
    valuation_times  jsonb NOT NULL DEFAULT '{}',
    fx_rates         jsonb NOT NULL DEFAULT '[]',
    stale_data       boolean NOT NULL DEFAULT false,
    data_version     text NOT NULL DEFAULT '',
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (snapshot_id),
    UNIQUE (period, day_epoch, version)
);

-- ============================================================================
-- asset_allocation_target — user-owned targets. actor CHECK blocks AI
-- writers at the schema level; AI output lands in suggestions jsonb or
-- the ai-assistant advisory store, never here.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.asset_allocation_target (
    target_id        bigint GENERATED ALWAYS AS IDENTITY,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    dimension        text NOT NULL
                     CHECK (dimension IN ('account','market','country',
                        'currency','asset_type','sector','instrument',
                        'strategy')),
    key              text NOT NULL,
    weight           numeric NOT NULL CHECK (weight >= 0 AND weight <= 1),
    drift_band       numeric NOT NULL DEFAULT 0.05,
    max_weight       numeric,
    set_by           text NOT NULL
                     CHECK (set_by NOT IN
                        ('ai','xingcheng','model','assistant')),
    set_at           timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (target_id),
    UNIQUE (dimension, key)
);

-- ============================================================================
-- asset_exposure_basket — ETF/fund constituent look-through files with
-- disclosure dates. Indirect exposure is analytical only — it is never
-- summed into portfolio market value.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.asset_exposure_basket (
    instrument_id    text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    constituents     jsonb NOT NULL DEFAULT '[]',  -- [{issuer|instrument_id, weight}]
    related_groups   jsonb NOT NULL DEFAULT '{}',  -- group → [instrument_id]
    disclosed_at     timestamptz NOT NULL,
    source           text NOT NULL DEFAULT 'MANUAL'
                     CHECK (source IN ('DEMO','MANUAL','FILE_IMPORT')),
    broker_confirmed boolean NOT NULL DEFAULT false
                     CHECK (NOT broker_confirmed),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id)
);

-- ============================================================================
-- asset_reconciliation — import/statement reconciliation reports:
-- classified diffs (new / quantity_changed / cost_changed /
-- cash_changed / duplicate / conflict) with batch provenance.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.asset_reconciliation (
    reconciliation_id text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    account_id       text NOT NULL,
    batch_id         text,
    statement_ref    text NOT NULL DEFAULT '',
    diff_summary     jsonb NOT NULL DEFAULT '{}',  -- kind → count
    items            jsonb NOT NULL DEFAULT '[]',
    status           text NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING','RESOLVED','FAILED')),
    created_at       timestamptz NOT NULL DEFAULT now(),
    resolved_at      timestamptz,
    PRIMARY KEY (reconciliation_id)
);

-- ============================================================================
-- asset_maintenance_run — observable maintenance history (status,
-- duration, findings, error isolation per job).
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.asset_maintenance_run (
    run_id           text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    job              text NOT NULL,
    status           text NOT NULL CHECK (status IN ('ok','error')),
    result           jsonb NOT NULL DEFAULT '{}',
    error            text,
    duration_ms      integer NOT NULL DEFAULT 0,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id)
);

-- ============================================================================
-- RLS + ownership (identical convention to 139/140/141)
-- ============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'asset_account_registry', 'asset_valuation',
        'asset_portfolio_snapshot', 'asset_allocation_target',
        'asset_exposure_basket', 'asset_reconciliation',
        'asset_maintenance_run'
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
