-- 004_global_and_module_version_tables.sql
-- Global version table (PostgreSQL central) + module version table.
-- Prevents upgrade desync between PostgreSQL central and SQLite local stores.
-- A44/E30 + A8/E21.

-- ============================================================================
-- Global version table — one row per schema migration applied to PostgreSQL.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.schema_version (
    version_id bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    migration_name text NOT NULL UNIQUE,
    applied_at timestamptz NOT NULL DEFAULT now(),
    applied_by text NOT NULL,
    checksum text NOT NULL
);

ALTER TABLE gptbridge_index.schema_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.schema_version FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS schema_version_read ON gptbridge_index.schema_version;
CREATE POLICY schema_version_read ON gptbridge_index.schema_version FOR SELECT
    USING (gptbridge_security.can_read('governance_rule')
           OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS schema_version_write ON gptbridge_index.schema_version;
CREATE POLICY schema_version_write ON gptbridge_index.schema_version FOR INSERT
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.schema_version FROM PUBLIC;
GRANT SELECT ON gptbridge_index.schema_version TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_index.schema_version TO gptbridge_index_executor;

-- ============================================================================
-- Module version table — one row per module's local schema version.
-- Each module declares its own schema_version here so the central authority
-- can detect desync between PostgreSQL and SQLite local stores.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.module_version (
    module_id text NOT NULL,
    store_type text NOT NULL CHECK (store_type IN ('postgresql', 'sqlite', 'ntfs')),
    schema_version integer NOT NULL CHECK (schema_version >= 1),
    integrity_hash text,
    last_reconciled_at timestamptz,
    last_reconciled_status text CHECK (
        last_reconciled_status IN ('in-sync', 'behind', 'ahead', 'conflict', 'unknown')
    ) DEFAULT 'unknown',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (module_id, store_type)
);

ALTER TABLE gptbridge_index.module_version ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.module_version FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS module_version_read ON gptbridge_index.module_version;
CREATE POLICY module_version_read ON gptbridge_index.module_version FOR SELECT
    USING (gptbridge_security.can_read(module_id));

DROP POLICY IF EXISTS module_version_write ON gptbridge_index.module_version;
CREATE POLICY module_version_write ON gptbridge_index.module_version FOR ALL
    USING (gptbridge_security.can_write(module_id))
    WITH CHECK (gptbridge_security.can_write(module_id));

REVOKE ALL ON gptbridge_index.module_version FROM PUBLIC;
GRANT SELECT ON gptbridge_index.module_version TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.module_version TO gptbridge_index_executor;

-- Index for reconciliation queries
CREATE INDEX IF NOT EXISTS module_version_reconcile_idx
    ON gptbridge_index.module_version (last_reconciled_status, updated_at);
