-- 036_wal_checkpoint_monitor.sql
-- WAL / Checkpoint Monitoring: track WAL generation rate, checkpoint
-- duration, buffers written, fsync latency, and WAL directory growth.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- wal_checkpoint_snapshot — periodic WAL/checkpoint stats
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.wal_checkpoint_snapshot (
    snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    wal_size_bytes bigint NOT NULL DEFAULT 0,
    checkpoint_count bigint NOT NULL DEFAULT 0,
    checkpoint_duration_ms real NOT NULL DEFAULT 0,
    checkpoint_buffers_written bigint NOT NULL DEFAULT 0,
    checkpoint_sync_time_ms real NOT NULL DEFAULT 0,
    wal_segments_count integer NOT NULL DEFAULT 0,
    wal_rate_mb_per_min real NOT NULL DEFAULT 0,
    collected_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.wal_checkpoint_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.wal_checkpoint_snapshot FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS wal_snap_read ON gptbridge_index.wal_checkpoint_snapshot;
CREATE POLICY wal_snap_read ON gptbridge_index.wal_checkpoint_snapshot
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS wal_snap_write ON gptbridge_index.wal_checkpoint_snapshot;
CREATE POLICY wal_snap_write ON gptbridge_index.wal_checkpoint_snapshot
    FOR INSERT WITH CHECK (true);

REVOKE ALL ON gptbridge_index.wal_checkpoint_snapshot FROM PUBLIC;
GRANT SELECT ON gptbridge_index.wal_checkpoint_snapshot TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_index.wal_checkpoint_snapshot
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS wal_snap_collected_idx
    ON gptbridge_index.wal_checkpoint_snapshot (collected_at);

-- ============================================================================
-- record_wal_checkpoint_snapshot() — record a WAL/checkpoint snapshot
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_wal_checkpoint_snapshot(
    p_wal_size_bytes bigint,
    p_checkpoint_count bigint,
    p_checkpoint_duration_ms real,
    p_checkpoint_buffers_written bigint,
    p_checkpoint_sync_time_ms real,
    p_wal_segments_count integer,
    p_wal_rate_mb_per_min real
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.wal_checkpoint_snapshot (
        wal_size_bytes, checkpoint_count, checkpoint_duration_ms,
        checkpoint_buffers_written, checkpoint_sync_time_ms,
        wal_segments_count, wal_rate_mb_per_min
    )
    VALUES (
        p_wal_size_bytes, p_checkpoint_count, p_checkpoint_duration_ms,
        p_checkpoint_buffers_written, p_checkpoint_sync_time_ms,
        p_wal_segments_count, p_wal_rate_mb_per_min
    )
    RETURNING snapshot_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
