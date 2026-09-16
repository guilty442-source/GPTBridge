-- 025_sqlite_generation_fence.sql
-- SQLite Generation Fence: extend the cross-engine generation fence to
-- SQLite module-private databases.  The SQLite template already carries
-- backend_generation (added in 012_cross_engine_consistency.sql); this
-- migration documents the contract and adds a PostgreSQL-side record of
-- each SQLite database's last-known generation, so the coordinator can
-- detect stale SQLite stores after a restore/rebuild.
--
-- Codex basis:
--   A8/E21  — SQLite: owner-private-operational-state + bounded-degraded.
--   A44/E30 — four-functions-local; UNAVAILABLE:declare-closed-not-replace.
--   A46/E22 — Audit: mandatory-ledger.
--
-- This migration adds:
--   * gptbridge_index.sqlite_generation — one row per SQLite database,
--     tracking its last-known backend_generation and sync status.
--   * A function to bump and check SQLite generation.

-- ============================================================================
-- sqlite_generation table
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.sqlite_generation (
    module_id text NOT NULL,
    database_path text NOT NULL,
    backend_generation bigint NOT NULL DEFAULT 1,
    last_synced_at timestamptz,
    stale boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (module_id, database_path)
);

ALTER TABLE gptbridge_index.sqlite_generation ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.sqlite_generation FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS sqlite_generation_read ON gptbridge_index.sqlite_generation;
CREATE POLICY sqlite_generation_read ON gptbridge_index.sqlite_generation
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS sqlite_generation_write ON gptbridge_index.sqlite_generation;
CREATE POLICY sqlite_generation_write ON gptbridge_index.sqlite_generation
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.sqlite_generation FROM PUBLIC;
GRANT SELECT ON gptbridge_index.sqlite_generation TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.sqlite_generation
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS sqlite_generation_stale_idx
    ON gptbridge_index.sqlite_generation (stale)
    WHERE stale = true;

-- ============================================================================
-- upsert_sqlite_generation() — called by the runtime after syncing a
-- SQLite database's generation.  Marks stale if the SQLite generation
-- is behind the current backend generation.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.upsert_sqlite_generation(
    p_module_id text,
    p_database_path text,
    p_generation bigint
) RETURNS void AS $$
DECLARE
    v_current_gen bigint;
BEGIN
    v_current_gen := gptbridge_index.current_backend_generation();
    INSERT INTO gptbridge_index.sqlite_generation (
        module_id, database_path, backend_generation,
        last_synced_at, stale, updated_at
    )
    VALUES (
        p_module_id, p_database_path, p_generation,
        now(), p_generation < v_current_gen, now()
    )
    ON CONFLICT (module_id, database_path) DO UPDATE SET
        backend_generation = p_generation,
        last_synced_at = now(),
        stale = p_generation < v_current_gen,
        updated_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
