-- 051_transport_retention.sql
-- Transport Retention Policy.
--
-- tool_request easily bloats.  Retention by status:
--   pending / claimed  → stay in hot table until completed
--   completed          → 7-30 days in hot, then archive
--   failed             → 30-90 days in hot, then archive
--   dead-letter        → keep until human/governance review complete
--
-- After retention, move to archive — never direct DELETE.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- transport_retention_policy — per-status retention rules
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.transport_retention_policy (
    status text PRIMARY KEY,
    hot_retention_days integer NOT NULL,  -- days to keep in hot table
    archive_after_days integer NOT NULL,  -- days after which to archive
    can_purge boolean NOT NULL DEFAULT false,
    purge_after_days integer,  -- days after archive before purge eligible
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.transport_retention_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.transport_retention_policy FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS transport_retention_read ON gptbridge_index.transport_retention_policy;
CREATE POLICY transport_retention_read ON gptbridge_index.transport_retention_policy
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS transport_retention_write ON gptbridge_index.transport_retention_policy;
CREATE POLICY transport_retention_write ON gptbridge_index.transport_retention_policy
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.transport_retention_policy FROM PUBLIC;
GRANT SELECT ON gptbridge_index.transport_retention_policy TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.transport_retention_policy
    TO gptbridge_index_executor;

-- ============================================================================
-- Seed default retention policies
-- ============================================================================
INSERT INTO gptbridge_index.transport_retention_policy
    (status, hot_retention_days, archive_after_days, can_purge, purge_after_days, description)
VALUES
    ('queued',    0,   0,   false, NULL,  'pending — stay in hot until completed'),
    ('claimed',   0,   0,   false, NULL,  'claimed — stay in hot until completed'),
    ('completed', 7,   30,  true,  90,    'completed — 7d hot, 30d then archive, purge after 90d'),
    ('failed',    30,  90,  true,  180,   'failed — 30d hot, 90d then archive, purge after 180d'),
    ('dead_letter', 0,  0,  false, NULL,  'dead-letter — keep until governance review complete')
ON CONFLICT (status) DO NOTHING;

-- ============================================================================
-- get_transport_archive_eligible() — find requests eligible for archiving
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_transport_archive_eligible(
    p_limit integer DEFAULT 100
) RETURNS TABLE (
    request_id text,
    channel_id text,
    target_tool_id text,
    status text,
    updated_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT tr.request_id, tr.channel_id, tr.target_tool_id,
           tr.status::text, tr.updated_at
    FROM gptbridge_transport.tool_request tr
    INNER JOIN gptbridge_index.transport_retention_policy pol
        ON tr.status::text = pol.status
    WHERE tr.status IN ('completed', 'failed')
      AND tr.updated_at < now() - (pol.hot_retention_days || ' days')::interval
      AND NOT EXISTS (
          SELECT 1 FROM gptbridge_transport.tool_request_history h
          WHERE h.request_id = tr.request_id
      )
    ORDER BY tr.updated_at
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_transport_purge_eligible() — find archived requests eligible for purge
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_transport_purge_eligible(
    p_limit integer DEFAULT 100
) RETURNS TABLE (
    request_id text,
    archived_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT h.request_id, h.archived_at
    FROM gptbridge_transport.tool_request_history h
    INNER JOIN gptbridge_index.transport_retention_policy pol
        ON 'completed' = pol.status  -- history is always from completed/failed
    WHERE pol.can_purge = true
      AND h.archived_at < now() - (COALESCE(pol.purge_after_days, 365) || ' days')::interval
      AND NOT EXISTS (
          SELECT 1 FROM gptbridge_index.retention_hold rh
          WHERE rh.entity_type = 'transport'
            AND rh.entity_id = h.request_id
            AND rh.hold_active = true
      )
    ORDER BY h.archived_at
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
