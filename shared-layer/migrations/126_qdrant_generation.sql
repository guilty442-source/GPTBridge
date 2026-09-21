-- qdrant_generation.sql
-- Qdrant Generation Sync: track Qdrant collection generation in PostgreSQL
-- so the coordinator can detect stale Qdrant collections after restore/rebuild.
--
-- Codex basis:
--   A8/E21  — Qdrant: canonical semantic index.
--   A44/E30 — four-functions-local; UNAVAILABLE:declare-closed-not-replace.
--   A46/E22 — Audit: mandatory-ledger.
--
-- This migration adds:
--   * gptbridge_index.qdrant_generation — one row per Qdrant collection,
--     tracking its last-known backend_generation and sync status.
--   * Functions to upsert and check Qdrant generation.

-- ============================================================================
-- qdrant_generation table
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.qdrant_generation (
    collection_name text NOT NULL,
    backend_generation bigint NOT NULL DEFAULT 1,
    last_synced_at timestamptz,
    stale boolean NOT NULL DEFAULT false,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (collection_name)
);

ALTER TABLE gptbridge_index.qdrant_generation ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.qdrant_generation FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS qdrant_generation_read ON gptbridge_index.qdrant_generation;
CREATE POLICY qdrant_generation_read ON gptbridge_index.qdrant_generation
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS qdrant_generation_write ON gptbridge_index.qdrant_generation;
CREATE POLICY qdrant_generation_write ON gptbridge_index.qdrant_generation
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.qdrant_generation FROM PUBLIC;
GRANT SELECT ON gptbridge_index.qdrant_generation TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.qdrant_generation
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS qdrant_generation_stale_idx
    ON gptbridge_index.qdrant_generation (stale)
    WHERE stale = true;

-- ============================================================================
-- upsert_qdrant_generation() — called by the runtime after syncing a
-- Qdrant collection's generation.  Marks stale if the Qdrant generation
-- is behind the current backend generation.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.upsert_qdrant_generation(
    p_collection_name text,
    p_generation bigint
) RETURNS void AS $$
DECLARE
    v_current_gen bigint;
BEGIN
    v_current_gen := gptbridge_index.current_backend_generation();
    INSERT INTO gptbridge_index.qdrant_generation (
        collection_name, backend_generation,
        last_synced_at, stale, updated_at
    )
    VALUES (
        p_collection_name, p_generation,
        now(), p_generation < v_current_gen, now()
    )
    ON CONFLICT (collection_name) DO UPDATE SET
        backend_generation = p_generation,
        last_synced_at = now(),
        stale = p_generation < v_current_gen,
        updated_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;