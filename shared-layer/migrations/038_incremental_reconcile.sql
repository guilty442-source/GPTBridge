-- 038_incremental_reconcile.sql
-- Incremental Reconciliation: use revision / dirty flag / pending queue
-- instead of full-library scans.  Only sync resources that changed.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A44/E30 — four-functions-local.

-- ============================================================================
-- reconcile_pending_queue — resources awaiting reconciliation
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.reconcile_pending_queue (
    queue_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id text NOT NULL,
    resource_id uuid NOT NULL,
    source_revision bigint NOT NULL,
    dirty boolean NOT NULL DEFAULT true,
    enqueued_at timestamptz NOT NULL DEFAULT now(),
    last_reconciled_revision bigint,
    last_reconciled_at timestamptz,
    reconcile_attempts integer NOT NULL DEFAULT 0
);

ALTER TABLE gptbridge_index.reconcile_pending_queue ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.reconcile_pending_queue FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS reconcile_queue_read ON gptbridge_index.reconcile_pending_queue;
CREATE POLICY reconcile_queue_read ON gptbridge_index.reconcile_pending_queue
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS reconcile_queue_write ON gptbridge_index.reconcile_pending_queue;
CREATE POLICY reconcile_queue_write ON gptbridge_index.reconcile_pending_queue
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.reconcile_pending_queue FROM PUBLIC;
GRANT SELECT ON gptbridge_index.reconcile_pending_queue TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE, DELETE ON gptbridge_index.reconcile_pending_queue
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS reconcile_queue_dirty_idx
    ON gptbridge_index.reconcile_pending_queue (dirty, enqueued_at)
    WHERE dirty = true;
CREATE INDEX IF NOT EXISTS reconcile_queue_module_idx
    ON gptbridge_index.reconcile_pending_queue (module_id, dirty);
CREATE INDEX IF NOT EXISTS reconcile_queue_resource_idx
    ON gptbridge_index.reconcile_pending_queue (resource_id);

-- ============================================================================
-- enqueue_reconcile_pending() — mark a resource as needing reconciliation
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.enqueue_reconcile_pending(
    p_module_id text,
    p_resource_id uuid,
    p_source_revision bigint
) RETURNS void AS $$
BEGIN
    INSERT INTO gptbridge_index.reconcile_pending_queue (
        module_id, resource_id, source_revision, dirty
    )
    VALUES (
        p_module_id, p_resource_id, p_source_revision, true
    )
    ON CONFLICT DO NOTHING;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- mark_reconciled() — mark a resource as reconciled at a given revision
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.mark_reconciled(
    p_module_id text,
    p_resource_id uuid,
    p_reconciled_revision bigint
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.reconcile_pending_queue
    SET dirty = false,
        last_reconciled_revision = p_reconciled_revision,
        last_reconciled_at = now(),
        reconcile_attempts = reconcile_attempts + 1
    WHERE module_id = p_module_id AND resource_id = p_resource_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_pending_reconcile() — fetch pending resources for a module
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_pending_reconcile(
    p_module_id text,
    p_limit integer DEFAULT 100
) RETURNS TABLE (
    resource_id uuid,
    source_revision bigint,
    enqueued_at timestamptz,
    last_reconciled_revision bigint
) AS $$
BEGIN
    RETURN QUERY
    SELECT resource_id, source_revision, enqueued_at, last_reconciled_revision
    FROM gptbridge_index.reconcile_pending_queue
    WHERE module_id = p_module_id AND dirty = true
    ORDER BY enqueued_at
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- purge_reconciled() — remove old reconciled entries
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.purge_reconciled(
    p_older_than_hours integer DEFAULT 24
) RETURNS integer AS $$
DECLARE
    v_count integer;
BEGIN
    DELETE FROM gptbridge_index.reconcile_pending_queue
    WHERE dirty = false
      AND last_reconciled_at < now() - (p_older_than_hours || ' hours')::interval;

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
