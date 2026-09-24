-- 135_market_data.sql
-- 星澄 AI 投資管理與自動操盤系統 — market data center schema.
--
-- Authoritative market-data records: source registry, normalized quotes,
-- candles, trading calendars, corporate actions, FX rates, revision
-- tracking and incremental sync cursors. Owned by ai-assistant
-- (module_id = 'ai-assistant'); the investment-mobile engine journal is
-- a runtime mirror and forwards records through the governed channel.
--
-- Codex basis: same 008 pattern — RLS + module-scoped writes; all
-- timestamps timestamptz (timezone-aware); prices/volumes numeric
-- (never float — settlement-authoritative precision).

CREATE SCHEMA IF NOT EXISTS gptbridge_trading;

-- ============================================================================
-- market_data_source — replaceable source registry + capability matrix
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.market_data_source (
    source_id        text        NOT NULL,
    module_id        text        NOT NULL DEFAULT 'ai-assistant',
    markets          jsonb       NOT NULL DEFAULT '[]'::jsonb,
    realtime         boolean     NOT NULL DEFAULT false,
    delayed          boolean     NOT NULL DEFAULT true,
    delay_minutes    int         NOT NULL DEFAULT 0,
    history_timeframes jsonb     NOT NULL DEFAULT '["1d"]'::jsonb,
    sessions         jsonb       NOT NULL DEFAULT '["regular"]'::jsonb,
    verified         boolean     NOT NULL DEFAULT false,
    legal_basis      text,
    connection_status text       NOT NULL DEFAULT 'disconnected',
    last_update      timestamptz,
    latency_ms       int,
    stale            boolean     NOT NULL DEFAULT true,
    error_code       text,
    recovery_status  text        NOT NULL DEFAULT 'idle',
    consecutive_failures int     NOT NULL DEFAULT 0,
    notes            text,
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (source_id)
);

-- ============================================================================
-- market_quote — latest quote per instrument (upsert surface).
--   Tick history is NOT retained here — see market_candle for persisted
--   series; unbounded per-tick accumulation is prohibited by policy.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.market_quote (
    instrument_id      text        NOT NULL,
    module_id          text        NOT NULL DEFAULT 'ai-assistant',
    market             text        NOT NULL,
    source_id          text        NOT NULL REFERENCES gptbridge_trading.market_data_source(source_id),
    source_timestamp   timestamptz NOT NULL,
    received_timestamp timestamptz NOT NULL DEFAULT now(),
    bid_price          numeric,
    ask_price          numeric,
    last_price         numeric,
    volume             numeric,
    currency           text,
    market_session     text        NOT NULL DEFAULT 'regular',
    data_status        text        NOT NULL DEFAULT 'ok',  -- ok|delayed|stale|suspect|missing
    PRIMARY KEY (instrument_id)
);
CREATE INDEX IF NOT EXISTS market_quote_market_idx
    ON gptbridge_trading.market_quote (market, data_status);

-- ============================================================================
-- market_candle — historical OHLCV, partitioned monthly on candle_start
--   Dedup key: (instrument_id, timeframe, candle_start, adjustment_type)
--   Corrections supersede via higher data_revision — full lineage in
--   market_data_revision.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.market_candle (
    instrument_id    text        NOT NULL,
    module_id        text        NOT NULL DEFAULT 'ai-assistant',
    market           text        NOT NULL,
    timeframe        text        NOT NULL,            -- 1d | 1m
    open             numeric     NOT NULL,
    high             numeric     NOT NULL,
    low              numeric     NOT NULL,
    close            numeric     NOT NULL,
    volume           numeric     NOT NULL,
    turnover         numeric     NOT NULL DEFAULT 0,
    candle_start     timestamptz NOT NULL,
    candle_end       timestamptz NOT NULL,
    source_id        text        NOT NULL REFERENCES gptbridge_trading.market_data_source(source_id),
    currency         text,
    data_revision    int         NOT NULL DEFAULT 1,
    adjustment_type  text        NOT NULL DEFAULT 'raw',  -- raw|split_adjusted|total_return
    received_at      timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (instrument_id, timeframe, candle_start, adjustment_type)
) PARTITION BY RANGE (candle_start);

-- Default partition catches anything; monthly partitions are added by
-- the maintenance pass as ranges arrive (no unbounded growth in one
-- table; old partitions can be detached + archived by retention).
CREATE TABLE IF NOT EXISTS gptbridge_trading.market_candle_default
    PARTITION OF gptbridge_trading.market_candle DEFAULT;

CREATE INDEX IF NOT EXISTS market_candle_lookup_idx
    ON gptbridge_trading.market_candle (instrument_id, timeframe, candle_start DESC);

-- ============================================================================
-- market_calendar — trading days / sessions / overrides per market
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.market_calendar (
    calendar_id    text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    market         text        NOT NULL,
    day            date        NOT NULL,
    closed         boolean     NOT NULL DEFAULT false,
    reason         text,
    source         text        NOT NULL DEFAULT 'operator',
    updated_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (calendar_id),
    UNIQUE (market, day)
);

-- ============================================================================
-- corporate_action — splits / dividends; raw prices never rewritten
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.corporate_action (
    action_id      text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    instrument_id  text        NOT NULL REFERENCES gptbridge_trading.instrument(instrument_id),
    kind           text        NOT NULL,   -- split|reverse_split|cash_dividend|stock_dividend|etf_distribution|other
    effective_date date        NOT NULL,
    ratio          numeric     NOT NULL DEFAULT 1,
    cash_amount    numeric     NOT NULL DEFAULT 0,
    source_id      text        NOT NULL,
    revision       int         NOT NULL DEFAULT 1,
    recorded_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (action_id)
);
CREATE INDEX IF NOT EXISTS corporate_action_idx
    ON gptbridge_trading.corporate_action (instrument_id, effective_date);

-- ============================================================================
-- currency_rate — FX with provenance (TWD/USD phase 1)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.currency_rate (
    rate_id        text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    base           text        NOT NULL,
    quote          text        NOT NULL,
    rate           numeric     NOT NULL,
    source_id      text        NOT NULL,
    observed_at    timestamptz NOT NULL,
    valid_until    timestamptz,
    created_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (rate_id)
);
CREATE INDEX IF NOT EXISTS currency_rate_pair_idx
    ON gptbridge_trading.currency_rate (base, quote, observed_at DESC);

-- ============================================================================
-- market_data_revision — every historical correction, tracked
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.market_data_revision (
    revision_id    text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    instrument_id  text        NOT NULL,
    timeframe      text        NOT NULL,
    candle_start   timestamptz NOT NULL,
    old_revision   int         NOT NULL,
    new_revision   int         NOT NULL,
    source_id      text        NOT NULL,
    reason         text,
    at             timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (revision_id)
);
CREATE INDEX IF NOT EXISTS market_data_revision_idx
    ON gptbridge_trading.market_data_revision (instrument_id, candle_start);

-- ============================================================================
-- market_data_sync_state — incremental cursors (never restart from zero)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.market_data_sync_state (
    source_id      text        NOT NULL,
    instrument_id  text        NOT NULL,
    timeframe      text        NOT NULL,
    module_id      text        NOT NULL DEFAULT 'ai-assistant',
    cursor         timestamptz,
    last_sync_at   timestamptz NOT NULL DEFAULT now(),
    status         text        NOT NULL DEFAULT 'ok',  -- ok|failed|resyncing
    error_code     text,
    rows_written   int         NOT NULL DEFAULT 0,
    PRIMARY KEY (source_id, instrument_id, timeframe)
);

-- ============================================================================
-- Seed the declared source capability matrix (all unverified until the
-- official API + licence are validated through the governed path)
-- ============================================================================
INSERT INTO gptbridge_trading.market_data_source
    (source_id, markets, realtime, delayed, delay_minutes,
     history_timeframes, sessions, verified, legal_basis, notes)
VALUES
    ('twse-openapi', '["tw"]', false, true, 0, '["1d"]', '["regular"]',
     false, 'TWSE OpenAPI (official public EOD data)',
     'intraday requires licensed feed'),
    ('tpex-openapi', '["tw"]', false, true, 0, '["1d"]', '["regular"]',
     false, 'TPEx OpenAPI (official public EOD data)', NULL),
    ('fugle', '["tw"]', true, false, 0, '["1d","1m"]', '["regular"]',
     false, 'Fugle official API (paid realtime tier)',
     'credential required'),
    ('polygon', '["us"]', true, false, 0, '["1d","1m"]',
     '["pre","regular","post"]', false,
     'Polygon.io commercial licence', 'US realtime incl. pre/post'),
    ('alphavantage', '["us"]', false, true, 15, '["1d","1m"]',
     '["regular"]', false, 'Alpha Vantage free/premium tiers',
     'delayed on free tier'),
    ('broker-feed', '["tw","us"]', false, true, 0, '["1d"]',
     '["regular"]', false, 'broker-provided quotes',
     'scope NOT assumed — verify separately from trading API'),
    ('manual-import', '["tw","us","fund"]', false, true, 0, '["1d"]',
     '["regular"]', true, 'operator-entered observations',
     'operator is responsible for provenance'),
    ('simulated', '["tw","us","fund"]', true, false, 0, '["1d","1m"]',
     '["regular"]', true, 'reproducible generated data — testing only',
     'never used for real decisions')
ON CONFLICT (source_id) DO NOTHING;

-- ============================================================================
-- RLS + grants (008 pattern)
-- ============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'market_data_source', 'market_quote', 'market_calendar',
        'corporate_action', 'currency_rate', 'market_data_revision',
        'market_data_sync_state'
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

    -- partitioned table: RLS applies to partitions through the parent
    EXECUTE 'ALTER TABLE gptbridge_trading.market_candle ENABLE ROW LEVEL SECURITY';
    EXECUTE 'ALTER TABLE gptbridge_trading.market_candle FORCE ROW LEVEL SECURITY';
    EXECUTE 'DROP POLICY IF EXISTS market_candle_read ON gptbridge_trading.market_candle';
    EXECUTE 'CREATE POLICY market_candle_read ON gptbridge_trading.market_candle
             FOR SELECT USING (gptbridge_security.can_read(module_id))';
    EXECUTE 'DROP POLICY IF EXISTS market_candle_write ON gptbridge_trading.market_candle';
    EXECUTE 'CREATE POLICY market_candle_write ON gptbridge_trading.market_candle
             FOR ALL
             USING (gptbridge_security.can_write(module_id))
             WITH CHECK (
                 gptbridge_security.can_write(module_id)
                 AND module_id = current_setting(''app.current_module_id'', true)
             )';
    EXECUTE 'REVOKE ALL ON gptbridge_trading.market_candle FROM PUBLIC';
    EXECUTE 'GRANT SELECT ON gptbridge_trading.market_candle TO gptbridge_index_reader';
    EXECUTE 'GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_trading.market_candle TO gptbridge_index_executor';
END $$;
