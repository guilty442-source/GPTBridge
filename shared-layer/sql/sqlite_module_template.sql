-- sqlite_module_template.sql
-- Canonical SQLite module-private database template.
-- Every module-owned SQLite database MUST apply this template before
-- creating business tables.  This ensures all 70+ SQLite databases share
-- a common metadata foundation.
-- A44/E30 + A8/E21.

-- ============================================================================
-- schema_version — single row, tracks this database's schema version.
-- ============================================================================
CREATE TABLE IF NOT EXISTS schema_version (
    schema_version INTEGER NOT NULL CHECK (schema_version >= 1),
    applied_at TEXT NOT NULL DEFAULT (datetime('now')),
    integrity_hash TEXT,
    PRIMARY KEY (schema_version)
);

-- Insert initial version 1 if empty
INSERT OR IGNORE INTO schema_version (schema_version)
SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM schema_version);

-- ============================================================================
-- module_metadata — single row, declares which module owns this database.
-- ============================================================================
CREATE TABLE IF NOT EXISTS module_metadata (
    module_id TEXT NOT NULL,
    store_type TEXT NOT NULL DEFAULT 'sqlite',
    platform_id TEXT NOT NULL DEFAULT 'local-model-platform',
    owner_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    integrity_hash TEXT,
    PRIMARY KEY (module_id)
);

-- ============================================================================
-- resource_metadata — canonical resource metadata for every resource row.
-- Uses the fixed field names from the metadata contract:
--   module_id / resource_id / locator_id / version / content_hash /
--   updated_at / status
-- ============================================================================
CREATE TABLE IF NOT EXISTS resource_metadata (
    module_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    locator_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    content_hash TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (
        status IN ('active', 'indexed', 'pending', 'missing',
                   'moving', 'archived', 'deleted', 'referenced')
    ),
    platform_id TEXT NOT NULL DEFAULT 'local-model-platform',
    owner_id TEXT NOT NULL,
    data_category TEXT,
    resource_type TEXT,
    resource_label TEXT,
    logical_key TEXT,
    classification TEXT,
    index_status TEXT,
    metadata TEXT,
    PRIMARY KEY (module_id, resource_id)
);

CREATE INDEX IF NOT EXISTS resource_metadata_locator_idx
    ON resource_metadata (locator_id);
CREATE INDEX IF NOT EXISTS resource_metadata_status_idx
    ON resource_metadata (module_id, status);
CREATE INDEX IF NOT EXISTS resource_metadata_updated_idx
    ON resource_metadata (updated_at);

-- ============================================================================
-- audit_event — append-only audit log for this module's database.
-- No UPDATE or DELETE trigger enforced (SQLite triggers are optional);
-- the application layer MUST treat this as append-only (A46/E22).
-- ============================================================================
CREATE TABLE IF NOT EXISTS audit_event (
    event_id TEXT NOT NULL PRIMARY KEY,
    module_id TEXT NOT NULL,
    resource_id TEXT,
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS audit_event_module_idx
    ON audit_event (module_id, created_at);

-- ============================================================================
-- reconcile_state — tracks reconciliation status with PostgreSQL central.
-- When PostgreSQL recovers from downtime, this table records which local
-- changes need to be reconciled (one-directional: SQLite → PostgreSQL).
-- ============================================================================
CREATE TABLE IF NOT EXISTS reconcile_state (
    module_id TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    local_version INTEGER NOT NULL,
    local_updated_at TEXT NOT NULL,
    local_content_hash TEXT,
    reconcile_status TEXT NOT NULL DEFAULT 'pending' CHECK (
        reconcile_status IN ('pending', 'in-sync', 'conflict', 'skipped')
    ),
    reconciled_at TEXT,
    PRIMARY KEY (module_id, resource_id)
);

CREATE INDEX IF NOT EXISTS reconcile_state_pending_idx
    ON reconcile_state (reconcile_status, local_updated_at)
    WHERE reconcile_status = 'pending';
