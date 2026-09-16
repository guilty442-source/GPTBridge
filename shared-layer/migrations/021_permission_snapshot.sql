-- 021_permission_snapshot.sql
-- Permission Proof Snapshot: when an important write occurs, save the
-- permission/RLS decision summary at that moment, so future rule changes
-- can still explain why the write was allowed.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger; write=governed-executor.
--   A10/E10 — Authorization: explicit-allowlist; deny-by-default.
--   A8/E21  — PostgreSQL: central-structured-official-data.
--
-- This migration adds:
--   * gptbridge_audit.permission_snapshot — one row per audited write,
--     capturing the evaluated RLS policies, role memberships, decision
--     summary, and the actor's effective permissions at write time.
--   * A helper function gptbridge_security.capture_permission_snapshot()
--     that runtime code calls before important writes to record the
--     current permission state.

-- ============================================================================
-- permission_snapshot table
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_audit.permission_snapshot (
    snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id uuid REFERENCES gptbridge_audit.event(event_id) ON DELETE CASCADE,
    actor_id text NOT NULL,
    session_user text NOT NULL,
    target_module text NOT NULL,
    target_resource_id text,
    target_classification text,
    evaluated_roles jsonb NOT NULL DEFAULT '[]'::jsonb,
    evaluated_policies jsonb NOT NULL DEFAULT '[]'::jsonb,
    decision_summary jsonb NOT NULL DEFAULT '{}'::jsonb,
    rls_context jsonb NOT NULL DEFAULT '{}'::jsonb,
    can_read boolean NOT NULL DEFAULT false,
    can_write boolean NOT NULL DEFAULT false,
    can_write_resource boolean NOT NULL DEFAULT false,
    captured_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_audit.permission_snapshot ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_audit.permission_snapshot FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS permission_snapshot_read ON gptbridge_audit.permission_snapshot;
CREATE POLICY permission_snapshot_read ON gptbridge_audit.permission_snapshot
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS permission_snapshot_insert ON gptbridge_audit.permission_snapshot;
CREATE POLICY permission_snapshot_insert ON gptbridge_audit.permission_snapshot
    FOR INSERT WITH CHECK (EXISTS (
        SELECT 1 FROM gptbridge_security.principal
        WHERE pg_has_role(current_user, role_name, 'member') AND audit_write
    ));

REVOKE ALL ON gptbridge_audit.permission_snapshot FROM PUBLIC;
GRANT SELECT ON gptbridge_audit.permission_snapshot TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_audit.permission_snapshot TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS permission_snapshot_event_idx
    ON gptbridge_audit.permission_snapshot (event_id)
    WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS permission_snapshot_actor_idx
    ON gptbridge_audit.permission_snapshot (actor_id, captured_at);
CREATE INDEX IF NOT EXISTS permission_snapshot_module_idx
    ON gptbridge_audit.permission_snapshot (target_module, captured_at);

-- ============================================================================
-- capture_permission_snapshot() — runtime helper.
-- Called before an important write to record the current permission state.
-- Returns the snapshot_id so the caller can link it to the audit event.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_security.capture_permission_snapshot(
    p_actor_id text,
    p_target_module text,
    p_target_resource_id text DEFAULT NULL,
    p_target_classification text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_snapshot_id uuid;
    v_roles jsonb;
    v_policies jsonb;
    v_can_read boolean;
    v_can_write boolean;
    v_can_write_resource boolean;
    v_session_user text;
BEGIN
    v_session_user := current_user;

    -- Collect roles the session user belongs to
    SELECT COALESCE(jsonb_agg(role_name), '[]'::jsonb)
    INTO v_roles
    FROM gptbridge_security.principal
    WHERE pg_has_role(v_session_user, role_name, 'member');

    -- Evaluate permissions at this moment
    v_can_read := gptbridge_security.can_read(p_target_module);
    v_can_write := gptbridge_security.can_write(p_target_module);
    IF p_target_classification IS NOT NULL THEN
        v_can_write_resource := gptbridge_security.can_write_resource(
            p_target_module, p_target_classification
        );
    ELSE
        v_can_write_resource := v_can_write;
    END IF;

    -- Collect evaluated policies (which principal rows matched)
    SELECT COALESCE(jsonb_agg(jsonb_build_object(
        'role_name', principal.role_name,
        'global_read', principal.global_read,
        'transport_execute', principal.transport_execute,
        'audit_write', principal.audit_write,
        'scope_can_read', COALESCE(scope.can_read, false),
        'scope_can_write', COALESCE(scope.can_write, false)
    )), '[]'::jsonb)
    INTO v_policies
    FROM gptbridge_security.principal
    LEFT JOIN gptbridge_security.principal_scope scope
      ON scope.role_name = principal.role_name
     AND scope.module_id = p_target_module
    WHERE pg_has_role(v_session_user, principal.role_name, 'member');

    INSERT INTO gptbridge_audit.permission_snapshot (
        actor_id, session_user, target_module, target_resource_id,
        target_classification, evaluated_roles, evaluated_policies,
        decision_summary, rls_context,
        can_read, can_write, can_write_resource
    )
    VALUES (
        p_actor_id, v_session_user, p_target_module, p_target_resource_id,
        p_target_classification, v_roles, v_policies,
        jsonb_build_object(
            'allowed', v_can_write_resource,
            'reason', CASE WHEN v_can_write_resource THEN 'explicit-grant' ELSE 'deny-by-default' END
        ),
        jsonb_build_object(
            'session_user', v_session_user,
            'target_module', p_target_module,
            'target_classification', p_target_classification
        ),
        v_can_read, v_can_write, v_can_write_resource
    )
    RETURNING snapshot_id INTO v_snapshot_id;

    RETURN v_snapshot_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_security, gptbridge_audit;
