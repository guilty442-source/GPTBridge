-- 052_audit_retention_layering.sql
-- Audit Retention Layering.
--
-- Central gptbridge_audit.event is an important ledger.
--   recent audit   → PostgreSQL hot partition
--   older audit    → archive partition
--   long-term audit → compressed archive
--
-- Append-only must be maintained even after archive:
--   event_id, sequence, correlation_id, decision_id, hash, timestamp
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- audit_retention_layer — per-layer retention rules
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.audit_retention_layer (
    layer_name text PRIMARY KEY,  -- 'hot', 'archive', 'long_term'
    retention_days integer NOT NULL,  -- days to keep in this layer
    next_layer text,  -- where to move after this layer
    compression_enabled boolean NOT NULL DEFAULT false,
    description text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.audit_retention_layer ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.audit_retention_layer FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS audit_retention_read ON gptbridge_index.audit_retention_layer;
CREATE POLICY audit_retention_read ON gptbridge_index.audit_retention_layer
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS audit_retention_write ON gptbridge_index.audit_retention_layer;
CREATE POLICY audit_retention_write ON gptbridge_index.audit_retention_layer
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.audit_retention_layer FROM PUBLIC;
GRANT SELECT ON gptbridge_index.audit_retention_layer TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.audit_retention_layer
    TO gptbridge_index_executor;

-- Seed default layers
INSERT INTO gptbridge_index.audit_retention_layer
    (layer_name, retention_days, next_layer, compression_enabled, description)
VALUES
    ('hot',       90,  'archive',    false, 'recent audit in hot PostgreSQL'),
    ('archive',   365,  'long_term', true,  'older audit in archive partition'),
    ('long_term', 3650, NULL,        true,  'long-term compressed archive')
ON CONFLICT (layer_name) DO NOTHING;

-- ============================================================================
-- get_audit_archive_eligible() — find events eligible for archiving
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_audit_archive_eligible(
    p_limit integer DEFAULT 1000
) RETURNS TABLE (
    event_id uuid,
    event_type text,
    occurred_at timestamptz
) AS $$
DECLARE
    v_hot_days integer;
BEGIN
    SELECT retention_days INTO v_hot_days
    FROM gptbridge_index.audit_retention_layer
    WHERE layer_name = 'hot';

    RETURN QUERY
    SELECT e.event_id, e.event_type, e.occurred_at
    FROM gptbridge_audit.event e
    WHERE e.occurred_at < now() - (v_hot_days || ' days')::interval
      AND NOT EXISTS (
          SELECT 1 FROM gptbridge_audit.event_history h
          WHERE h.event_id = e.event_id
      )
      AND NOT EXISTS (
          SELECT 1 FROM gptbridge_index.retention_hold rh
          WHERE rh.entity_type = 'audit'
            AND rh.entity_id = e.event_id::text
            AND rh.hold_active = true
      )
    ORDER BY e.occurred_at
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_audit_long_term_eligible() — find archived events eligible for long-term
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_audit_long_term_eligible(
    p_limit integer DEFAULT 1000
) RETURNS TABLE (
    event_id uuid,
    archived_at timestamptz
) AS $$
DECLARE
    v_archive_days integer;
BEGIN
    SELECT retention_days INTO v_archive_days
    FROM gptbridge_index.audit_retention_layer
    WHERE layer_name = 'archive';

    RETURN QUERY
    SELECT h.event_id, h.archived_at
    FROM gptbridge_audit.event_history h
    WHERE h.archived_at < now() - (v_archive_days || ' days')::interval
    ORDER BY h.archived_at
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
