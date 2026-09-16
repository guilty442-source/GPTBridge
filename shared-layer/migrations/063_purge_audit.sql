-- 063_purge_audit.sql
-- Purge Audit.
--
-- When permanent deletion occurs, record:
--   purge_id, resource_id, actor, executor, approval,
--   previous_hash, deleted_from, deleted_at, verification_result
--
-- Central audit keeps evidence, but does NOT keep the deleted sensitive
-- original data.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- purge_audit_log — audit trail for permanent deletions
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.purge_audit_log (
    purge_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    resource_id text NOT NULL,
    entity_type text NOT NULL DEFAULT 'resource',
    module_id text,
    actor text NOT NULL,           -- who requested
    executor text NOT NULL,        -- what executed the purge
    approval_id uuid,              -- governance approval reference
    purge_queue_id uuid REFERENCES gptbridge_index.purge_queue(queue_id),
    previous_hash text,           -- hash of data before purge (for verification)
    deleted_from text NOT NULL,   -- 'postgresql', 'sqlite', 'qdrant'
    deleted_from_detail jsonb,    -- specific tables/collections
    deleted_at timestamptz NOT NULL DEFAULT now(),
    verification_result jsonb,    -- post-purge verification
    verified boolean NOT NULL DEFAULT false,
    verified_at timestamptz,
    audit_hash text NOT NULL,      -- hash of this audit record itself
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.purge_audit_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.purge_audit_log FORCE ROW LEVEL SECURITY;

-- Append-only: only INSERT allowed, no UPDATE/DELETE
DROP POLICY IF EXISTS purge_audit_read ON gptbridge_index.purge_audit_log;
CREATE POLICY purge_audit_read ON gptbridge_index.purge_audit_log
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS purge_audit_insert ON gptbridge_index.purge_audit_log;
CREATE POLICY purge_audit_insert ON gptbridge_index.purge_audit_log
    FOR INSERT WITH CHECK (gptbridge_security.can_write('governance_rule'));

-- No UPDATE or DELETE policy — append-only
REVOKE ALL ON gptbridge_index.purge_audit_log FROM PUBLIC;
GRANT SELECT ON gptbridge_index.purge_audit_log TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_index.purge_audit_log
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS purge_audit_resource_idx
    ON gptbridge_index.purge_audit_log (resource_id, deleted_at);
CREATE INDEX IF NOT EXISTS purge_audit_executor_idx
    ON gptbridge_index.purge_audit_log (executor, deleted_at);
CREATE INDEX IF NOT EXISTS purge_audit_date_idx
    ON gptbridge_index.purge_audit_log (deleted_at);

-- ============================================================================
-- record_purge() — record a permanent deletion
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_purge(
    p_resource_id text,
    p_actor text,
    p_executor text,
    p_deleted_from text,
    p_previous_hash text,
    p_deleted_from_detail jsonb DEFAULT NULL,
    p_approval_id uuid DEFAULT NULL,
    p_purge_queue_id uuid DEFAULT NULL,
    p_verification_result jsonb DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_hash text;
    v_entity_type text := 'resource';
    v_module_id text := NULL;
BEGIN
    -- Get entity type and module from purge queue if available
    IF p_purge_queue_id IS NOT NULL THEN
        SELECT entity_type, module_id INTO v_entity_type, v_module_id
        FROM gptbridge_index.purge_queue
        WHERE queue_id = p_purge_queue_id;
    END IF;

    -- Compute audit hash
    v_hash := md5(
        p_resource_id || '|' || p_actor || '|' || p_executor ||
        '|' || p_deleted_from || '|' || COALESCE(p_previous_hash, '') ||
        '|' || extract(epoch from now())::text
    );

    INSERT INTO gptbridge_index.purge_audit_log (
        resource_id, entity_type, module_id, actor, executor,
        approval_id, purge_queue_id, previous_hash,
        deleted_from, deleted_from_detail, verification_result,
        audit_hash
    )
    VALUES (
        p_resource_id, v_entity_type, v_module_id, p_actor, p_executor,
        p_approval_id, p_purge_queue_id, p_previous_hash,
        p_deleted_from, p_deleted_from_detail, p_verification_result,
        v_hash
    )
    RETURNING purge_id INTO v_id;

    -- Mark purge queue as completed
    IF p_purge_queue_id IS NOT NULL THEN
        PERFORM gptbridge_index.mark_purged(p_purge_queue_id, v_id);
    END IF;

    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- verify_purge() — verify a purge was completed correctly
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.verify_purge(
    p_purge_id uuid,
    p_verification_result jsonb
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.purge_audit_log
    SET verified = true,
        verified_at = now(),
        verification_result = p_verification_result
    WHERE purge_id = p_purge_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_purge_history() — get purge history for a resource
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_purge_history(
    p_resource_id text
) RETURNS TABLE (
    purge_id uuid,
    actor text,
    executor text,
    deleted_from text,
    deleted_at timestamptz,
    verified boolean
) AS $$
BEGIN
    RETURN QUERY
    SELECT purge_id, actor, executor, deleted_from, deleted_at, verified
    FROM gptbridge_index.purge_audit_log
    WHERE resource_id = p_resource_id
    ORDER BY deleted_at DESC;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
