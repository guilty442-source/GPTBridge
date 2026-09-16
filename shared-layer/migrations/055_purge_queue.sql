-- 055_purge_queue.sql
-- Tombstone Purge Queue.
--
-- Unified purge_queue records:
--   resource_id, module_id, requested_at, retention_until,
--   reason, requested_by, approval_id, purge_status
--
-- Purge only executes when retention expires, by local Cleanup Executor.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- purge_queue — unified purge queue
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.purge_queue (
    queue_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id text NOT NULL,
    module_id text,
    entity_type text NOT NULL DEFAULT 'resource',
    requested_at timestamptz NOT NULL DEFAULT now(),
    retention_until timestamptz NOT NULL,
    reason text NOT NULL,
    requested_by text NOT NULL,
    approval_id uuid,
    purge_status text NOT NULL DEFAULT 'pending' CHECK (purge_status IN (
        'pending', 'approved', 'retention_holding',
        'purge_eligible', 'purging', 'purged', 'rejected'
    )),
    purged_at timestamptz,
    purge_audit_id uuid,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.purge_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.purge_queue FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS purge_queue_read ON gptbridge_index.purge_queue;
CREATE POLICY purge_queue_read ON gptbridge_index.purge_queue
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS purge_queue_write ON gptbridge_index.purge_queue;
CREATE POLICY purge_queue_write ON gptbridge_index.purge_queue
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.purge_queue FROM PUBLIC;
GRANT SELECT ON gptbridge_index.purge_queue TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.purge_queue
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS purge_queue_status_idx
    ON gptbridge_index.purge_queue (purge_status, retention_until);
CREATE INDEX IF NOT EXISTS purge_queue_resource_idx
    ON gptbridge_index.purge_queue (resource_id);
CREATE INDEX IF NOT EXISTS purge_queue_retention_idx
    ON gptbridge_index.purge_queue (retention_until) WHERE purge_status = 'approved';

-- ============================================================================
-- enqueue_purge() — add a resource to the purge queue
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.enqueue_purge(
    p_resource_id text,
    p_reason text,
    p_requested_by text,
    p_retention_days integer DEFAULT 30,
    p_module_id text DEFAULT NULL,
    p_entity_type text DEFAULT 'resource',
    p_approval_id uuid DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.purge_queue (
        resource_id, module_id, entity_type, reason, requested_by,
        retention_until, approval_id
    )
    VALUES (
        p_resource_id, p_module_id, p_entity_type, p_reason, p_requested_by,
        now() + (p_retention_days || ' days')::interval, p_approval_id
    )
    RETURNING queue_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- approve_purge() — approve a purge request
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.approve_purge(
    p_queue_id uuid,
    p_approval_id uuid
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.purge_queue
    SET purge_status = 'approved',
        approval_id = p_approval_id,
        updated_at = now()
    WHERE queue_id = p_queue_id AND purge_status = 'pending';
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_purge_eligible() — find approved purges past retention
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_purge_eligible(
    p_limit integer DEFAULT 100
) RETURNS TABLE (
    queue_id uuid,
    resource_id text,
    module_id text,
    entity_type text,
    retention_until timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT queue_id, resource_id, module_id, entity_type, retention_until
    FROM gptbridge_index.purge_queue
    WHERE purge_status = 'approved'
      AND retention_until <= now()
      AND NOT EXISTS (
          SELECT 1 FROM gptbridge_index.retention_hold rh
          WHERE rh.entity_type = entity_type
            AND rh.entity_id = resource_id
            AND rh.hold_active = true
      )
    ORDER BY retention_until
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- mark_purged() — mark a purge as completed
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.mark_purged(
    p_queue_id uuid,
    p_purge_audit_id uuid DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.purge_queue
    SET purge_status = 'purged',
        purged_at = now(),
        purge_audit_id = p_purge_audit_id,
        updated_at = now()
    WHERE queue_id = p_queue_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
