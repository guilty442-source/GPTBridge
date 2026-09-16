-- 108_recovery_safety_fence.sql
-- Recovery Safety Fence.
--
-- Orchestrator must NEVER automatically:
--   DROP authoritative database, truncate official data,
--   rewrite governance codex, change RLS, grant elevated roles,
--   purge conflicting data
-- These require higher governance authority.
--
-- Codex basis:
--   A10/E10 — explicit-allowlist.
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.recovery_safety_fence (
    fence_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    forbidden_action text NOT NULL CHECK (forbidden_action IN (
        'drop_authoritative_database', 'truncate_official_data',
        'rewrite_governance_codex', 'change_rls_policy',
        'grant_elevated_role', 'purge_conflicting_data',
        'drop_migration_table', 'disable_row_level_security',
        'bypass_fail_closed', 'overwrite_audit_log'
    )),
    description text,
    requires_authority text NOT NULL DEFAULT 'manual_governance_approval',
    blocked_count integer NOT NULL DEFAULT 0,
    last_blocked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.recovery_safety_fence ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.recovery_safety_fence FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS recovery_fence_read ON gptbridge_index.recovery_safety_fence;
CREATE POLICY recovery_fence_read ON gptbridge_index.recovery_safety_fence
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS recovery_fence_write ON gptbridge_index.recovery_safety_fence;
CREATE POLICY recovery_fence_write ON gptbridge_index.recovery_safety_fence
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.recovery_safety_fence FROM PUBLIC;
GRANT SELECT ON gptbridge_index.recovery_safety_fence TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.recovery_safety_fence
    TO gptbridge_index_executor;

-- Pre-populate all forbidden actions
INSERT INTO gptbridge_index.recovery_safety_fence (forbidden_action, description) VALUES
    ('drop_authoritative_database', 'Never auto-DROP authoritative PostgreSQL database'),
    ('truncate_official_data', 'Never auto-purge official data tables via bulk delete'),
    ('rewrite_governance_codex', 'Never auto-rewrite governance codex files'),
    ('change_rls_policy', 'Never auto-change RLS policies during recovery'),
    ('grant_elevated_role', 'Never auto-grant elevated database roles'),
    ('purge_conflicting_data', 'Never auto-purge conflicting data'),
    ('drop_migration_table', 'Never auto-DROP migration tracking tables'),
    ('disable_row_level_security', 'Never auto-disable RLS'),
    ('bypass_fail_closed', 'Never auto-bypass fail-closed protections'),
    ('overwrite_audit_log', 'Never auto-overwrite or delete audit log entries')
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION gptbridge_index.check_safety_fence(
    p_action text
) RETURNS boolean AS $$
-- Returns true if action is FORBIDDEN (blocked)
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM gptbridge_index.recovery_safety_fence
        WHERE forbidden_action = p_action
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.record_fence_block(
    p_action text
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.recovery_safety_fence
    SET blocked_count = blocked_count + 1, last_blocked_at = now()
    WHERE forbidden_action = p_action;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
