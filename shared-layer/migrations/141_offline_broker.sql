-- ============================================================================
-- 141_offline_broker.sql — offline broker-integration foundation.
--
-- Schema: gptbridge_trading (business owner ai-assistant).
--
-- Phase rule: no real broker connection exists. Two complementary CHECKs:
--   * offline_account*/investment_import* rows carry
--     ``broker_confirmed boolean NOT NULL DEFAULT false
--       CHECK (NOT broker_confirmed)`` — manual/imported data can NEVER
--     masquerade as broker-verified truth.
--   * broker_simulation* rows carry
--     ``simulated boolean NOT NULL DEFAULT true CHECK (simulated)`` —
--     mock-environment records can NEVER be written as real fills.
-- Live-path tables (simulated=false) live in 140_live_trading.sql;
-- paper-path tables (simulated=true, paper-*) in 139_simulation_trading.sql.
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS gptbridge_trading;

-- ============================================================================
-- broker_profile — per-broker contract descriptor (capabilities, fee
-- model, session model, declared market traits; nothing verified)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.broker_profile (
    broker_id        text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    label            text NOT NULL,
    market           text NOT NULL,                 -- tw | us | fund
    account_kind     text NOT NULL,                 -- cathay-tw|fubon-us|fund
    capabilities     jsonb NOT NULL DEFAULT '{}',   -- fn → SUPPORTED|UNSUPPORTED|UNKNOWN
    fee_model        jsonb NOT NULL DEFAULT '{}',
    error_model      jsonb NOT NULL DEFAULT '{}',
    session_model    jsonb NOT NULL DEFAULT '{}',
    market_traits    jsonb NOT NULL DEFAULT '{}',
    connection_state text NOT NULL DEFAULT 'NOT_CONFIGURED'
                     CHECK (connection_state IN (
                        'NOT_CONFIGURED','OFFLINE','MOCK',
                        'READY_FOR_INTEGRATION')),
                    -- CONNECTED/DEGRADED/DISCONNECTED unreachable this phase
    api_verified     boolean NOT NULL DEFAULT false CHECK (NOT api_verified),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (broker_id)
);

-- ============================================================================
-- offline_account — manual/demo/file-imported books (never broker-synced)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.offline_account (
    account_id       text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    kind             text NOT NULL
                     CHECK (kind IN ('cathay-tw','fubon-us','fund')),
    label            text NOT NULL DEFAULT '',
    broker_id        text NOT NULL,
    market           text NOT NULL,
    currency         text NOT NULL,
    source           text NOT NULL
                     CHECK (source IN ('DEMO','MANUAL','FILE_IMPORT')),
                    -- LIVE_BROKER_SYNC is not a valid source this phase
    broker_confirmed boolean NOT NULL DEFAULT false
                     CHECK (NOT broker_confirmed),
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id)
);

CREATE TABLE IF NOT EXISTS gptbridge_trading.offline_holding (
    holding_id       text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    account_id       text NOT NULL REFERENCES
                     gptbridge_trading.offline_account(account_id),
    instrument_id    text NOT NULL,
    quantity         numeric NOT NULL,
    avg_cost         numeric NOT NULL DEFAULT 0,
    currency         text NOT NULL,
    source           text NOT NULL
                     CHECK (source IN ('DEMO','MANUAL','FILE_IMPORT')),
    broker_confirmed boolean NOT NULL DEFAULT false
                     CHECK (NOT broker_confirmed),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (holding_id)
);
CREATE INDEX IF NOT EXISTS offline_holding_acct_idx
    ON gptbridge_trading.offline_holding (account_id, instrument_id);

CREATE TABLE IF NOT EXISTS gptbridge_trading.offline_cash (
    entry_id         text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    account_id       text NOT NULL REFERENCES
                     gptbridge_trading.offline_account(account_id),
    currency         text NOT NULL,
    amount           numeric NOT NULL,
    source           text NOT NULL
                     CHECK (source IN ('DEMO','MANUAL','FILE_IMPORT')),
    broker_confirmed boolean NOT NULL DEFAULT false
                     CHECK (NOT broker_confirmed),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (entry_id)
);
CREATE INDEX IF NOT EXISTS offline_cash_acct_idx
    ON gptbridge_trading.offline_cash (account_id, currency);

CREATE TABLE IF NOT EXISTS gptbridge_trading.offline_transaction (
    txn_id           text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    account_id       text NOT NULL REFERENCES
                     gptbridge_trading.offline_account(account_id),
    instrument_id    text NOT NULL,
    side             text NOT NULL CHECK (side IN ('buy','sell')),
    quantity         numeric NOT NULL,
    price            numeric NOT NULL,
    fee              numeric NOT NULL DEFAULT 0,
    tax              numeric NOT NULL DEFAULT 0,
    traded_at        timestamptz NOT NULL,
    source           text NOT NULL
                     CHECK (source IN ('DEMO','MANUAL','FILE_IMPORT')),
    broker_confirmed boolean NOT NULL DEFAULT false
                     CHECK (NOT broker_confirmed),
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (txn_id)
);
CREATE INDEX IF NOT EXISTS offline_txn_acct_idx
    ON gptbridge_trading.offline_transaction (account_id, traded_at DESC);

CREATE TABLE IF NOT EXISTS gptbridge_trading.offline_dividend (
    dividend_id      text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    account_id       text NOT NULL REFERENCES
                     gptbridge_trading.offline_account(account_id),
    instrument_id    text NOT NULL,
    amount           numeric NOT NULL,
    currency         text NOT NULL,
    paid_at          timestamptz NOT NULL,
    source           text NOT NULL
                     CHECK (source IN ('DEMO','MANUAL','FILE_IMPORT')),
    broker_confirmed boolean NOT NULL DEFAULT false
                     CHECK (NOT broker_confirmed),
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dividend_id)
);
CREATE INDEX IF NOT EXISTS offline_dividend_acct_idx
    ON gptbridge_trading.offline_dividend (account_id, paid_at DESC);

-- ============================================================================
-- investment_import_batch / _row / _error — staged import evidence
-- (select → detect → preview → map → validate → dedup → confirm → commit;
--  every batch keeps a snapshot reference so it can be REVERTED)
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.investment_import_batch (
    batch_id         text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    account_id       text NOT NULL REFERENCES
                     gptbridge_trading.offline_account(account_id),
    file_name        text NOT NULL,
    file_format      text NOT NULL
                     CHECK (file_format IN ('csv','json','xlsx')),
    target           text NOT NULL CHECK (target IN
                     ('holdings','cash','transactions','dividends')),
    row_count        integer NOT NULL DEFAULT 0,
    written          integer NOT NULL DEFAULT 0,
    status           text NOT NULL DEFAULT 'PREVIEW'
                     CHECK (status IN ('PREVIEW','COMMITTED','FAILED',
                                       'REVERTED')),
    mapping          jsonb NOT NULL DEFAULT '{}',
    snapshot_ref     text NOT NULL DEFAULT '',
    created_at       timestamptz NOT NULL DEFAULT now(),
    committed_at     timestamptz,
    reverted_at      timestamptz,
    PRIMARY KEY (batch_id)
);

CREATE TABLE IF NOT EXISTS gptbridge_trading.investment_import_row (
    row_id           text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    batch_id         text NOT NULL REFERENCES
                     gptbridge_trading.investment_import_batch(batch_id),
    row_index        integer NOT NULL,
    content_hash     text NOT NULL,           -- dedup key
    raw              jsonb NOT NULL,
    status           text NOT NULL DEFAULT 'PENDING'
                     CHECK (status IN ('PENDING','WRITTEN','DUPLICATE',
                                       'REJECTED')),
    PRIMARY KEY (row_id)
);
CREATE INDEX IF NOT EXISTS import_row_batch_idx
    ON gptbridge_trading.investment_import_row (batch_id);
CREATE UNIQUE INDEX IF NOT EXISTS import_row_dedup_idx
    ON gptbridge_trading.investment_import_row
    (batch_id, content_hash) WHERE status = 'WRITTEN';

CREATE TABLE IF NOT EXISTS gptbridge_trading.investment_import_error (
    error_id         text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    batch_id         text NOT NULL REFERENCES
                     gptbridge_trading.investment_import_batch(batch_id),
    row_index        integer NOT NULL,
    error_code       text NOT NULL,
    detail           jsonb NOT NULL DEFAULT '{}',
    at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (error_id)
);

-- ============================================================================
-- broker_simulation / broker_simulation_event — mock-environment books.
-- ``simulated`` is CHECK-constrained TRUE: a mock fill can never be
-- recorded as a real execution.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_trading.broker_simulation (
    sim_id           text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    broker_id        text NOT NULL,               -- MOCK_CATHAY_TW|MOCK_FUBON_US
    market           text NOT NULL,
    label            text NOT NULL DEFAULT '',
    state            text NOT NULL DEFAULT 'MOCK'
                     CHECK (state = 'MOCK'),
    simulated        boolean NOT NULL DEFAULT true CHECK (simulated),
    created_at       timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (sim_id)
);

CREATE TABLE IF NOT EXISTS gptbridge_trading.broker_simulation_event (
    event_id         text NOT NULL,
    module_id        text NOT NULL DEFAULT 'ai-assistant',
    sim_id           text NOT NULL REFERENCES
                     gptbridge_trading.broker_simulation(sim_id),
    event_type       text NOT NULL,               -- order|fill|cancel|reject
    payload          jsonb NOT NULL DEFAULT '{}',
    simulated        boolean NOT NULL DEFAULT true CHECK (simulated),
    at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id)
);
CREATE INDEX IF NOT EXISTS broker_sim_event_idx
    ON gptbridge_trading.broker_simulation_event (sim_id, at);

-- ============================================================================
-- RLS + grants (008/134–140 pattern)
-- ============================================================================
DO $$
DECLARE
    t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'broker_profile', 'offline_account', 'offline_holding',
        'offline_cash', 'offline_transaction', 'offline_dividend',
        'investment_import_batch', 'investment_import_row',
        'investment_import_error', 'broker_simulation',
        'broker_simulation_event'
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
