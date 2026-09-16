-- 033_query_fingerprint.sql
-- Query Fingerprint Collection: collect query execution stats to drive
-- index decisions — not guess from schema.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- query_fingerprint — aggregated query execution stats
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.query_fingerprint (
    fingerprint_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    query_key text NOT NULL,  -- maps to query_allowlist key
    query_hash text NOT NULL,  -- SHA256 of normalized SQL
    execution_count bigint NOT NULL DEFAULT 0,
    total_latency_ms real NOT NULL DEFAULT 0,
    mean_latency_ms real NOT NULL DEFAULT 0,
    p95_latency_ms real NOT NULL DEFAULT 0,
    rows_returned_total bigint NOT NULL DEFAULT 0,
    rows_scanned_total bigint NOT NULL DEFAULT 0,
    shared_blocks_hit_total bigint NOT NULL DEFAULT 0,
    shared_blocks_read_total bigint NOT NULL DEFAULT 0,
    last_executed_at timestamptz,
    first_seen_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (query_key)
);

ALTER TABLE gptbridge_index.query_fingerprint ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.query_fingerprint FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS query_fp_read ON gptbridge_index.query_fingerprint;
CREATE POLICY query_fp_read ON gptbridge_index.query_fingerprint
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS query_fp_write ON gptbridge_index.query_fingerprint;
CREATE POLICY query_fp_write ON gptbridge_index.query_fingerprint
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.query_fingerprint FROM PUBLIC;
GRANT SELECT ON gptbridge_index.query_fingerprint TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.query_fingerprint
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS query_fp_key_idx
    ON gptbridge_index.query_fingerprint (query_key);
CREATE INDEX IF NOT EXISTS query_fp_latency_idx
    ON gptbridge_index.query_fingerprint (p95_latency_ms DESC)
    WHERE execution_count > 0;
CREATE INDEX IF NOT EXISTS query_fp_count_idx
    ON gptbridge_index.query_fingerprint (execution_count DESC)
    WHERE execution_count > 0;

-- ============================================================================
-- record_query_fingerprint() — upsert query execution stats
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_query_fingerprint(
    p_query_key text,
    p_query_hash text,
    p_latency_ms real,
    p_rows_returned bigint DEFAULT 0,
    p_rows_scanned bigint DEFAULT 0,
    p_shared_blocks_hit bigint DEFAULT 0,
    p_shared_blocks_read bigint DEFAULT 0
) RETURNS void AS $$
BEGIN
    INSERT INTO gptbridge_index.query_fingerprint (
        query_key, query_hash, execution_count,
        total_latency_ms, mean_latency_ms, p95_latency_ms,
        rows_returned_total, rows_scanned_total,
        shared_blocks_hit_total, shared_blocks_read_total,
        last_executed_at
    )
    VALUES (
        p_query_key, p_query_hash, 1,
        p_latency_ms, p_latency_ms, p_latency_ms,
        p_rows_returned, p_rows_scanned,
        p_shared_blocks_hit, p_shared_blocks_read,
        now()
    )
    ON CONFLICT (query_key) DO UPDATE SET
        execution_count = query_fingerprint.execution_count + 1,
        total_latency_ms = query_fingerprint.total_latency_ms + p_latency_ms,
        mean_latency_ms = (query_fingerprint.total_latency_ms + p_latency_ms) /
                         (query_fingerprint.execution_count + 1),
        p95_latency_ms = GREATEST(query_fingerprint.p95_latency_ms, p_latency_ms),
        rows_returned_total = query_fingerprint.rows_returned_total + p_rows_returned,
        rows_scanned_total = query_fingerprint.rows_scanned_total + p_rows_scanned,
        shared_blocks_hit_total = query_fingerprint.shared_blocks_hit_total + p_shared_blocks_hit,
        shared_blocks_read_total = query_fingerprint.shared_blocks_read_total + p_shared_blocks_read,
        last_executed_at = now(),
        updated_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_hot_queries() — return top queries by p95 latency or execution count
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_hot_queries(
    p_order_by text DEFAULT 'p95',
    p_limit integer DEFAULT 20
) RETURNS TABLE (
    query_key text,
    execution_count bigint,
    mean_latency_ms real,
    p95_latency_ms real,
    rows_returned_total bigint,
    rows_scanned_total bigint
) AS $$
BEGIN
    IF p_order_by = 'count' THEN
        RETURN QUERY
        SELECT query_key, execution_count, mean_latency_ms, p95_latency_ms,
               rows_returned_total, rows_scanned_total
        FROM gptbridge_index.query_fingerprint
        WHERE execution_count > 0
        ORDER BY execution_count DESC
        LIMIT p_limit;
    ELSE
        RETURN QUERY
        SELECT query_key, execution_count, mean_latency_ms, p95_latency_ms,
               rows_returned_total, rows_scanned_total
        FROM gptbridge_index.query_fingerprint
        WHERE execution_count > 0
        ORDER BY p95_latency_ms DESC
        LIMIT p_limit;
    END IF;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
