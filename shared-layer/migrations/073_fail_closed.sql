-- 073_fail_closed.sql
-- Fail-closed.
--
-- If integrity check fails:
--   governance codex hash mismatch
--   audit chain broken
--   schema contract hash mismatch
--   restore snapshot mismatch
--
-- Cannot silently continue writing.  Must enter:
--   read-only / quarantine / recovery
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A10/E10 — explicit-allowlist.

-- ============================================================================
-- fail_closed_action — records fail-closed actions taken
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.fail_closed_action (
    action_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    trigger_type text NOT NULL CHECK (trigger_type IN (
        'codex_hash_mismatch', 'audit_chain_broken',
        'schema_contract_mismatch', 'restore_snapshot_mismatch',
        'resource_hash_mismatch', 'sqlite_digest_mismatch',
        'qdrant_integrity_mismatch', 'merkle_root_mismatch',
        'manual_trigger'
    )),
    trigger_entity_id text,
    trigger_details jsonb,
    action_taken text NOT NULL CHECK (action_taken IN (
        'read_only', 'quarantine', 'recovery', 'block_writes', 'alert'
    )),
    domain text NOT NULL,  -- which domain was set to read-only/quarantine
    triggered_at timestamptz NOT NULL DEFAULT now(),
    triggered_by text NOT NULL,
    released_at timestamptz,
    released_by text,
    release_reason text,
    active boolean NOT NULL DEFAULT true
);

ALTER TABLE gptbridge_index.fail_closed_action ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.fail_closed_action FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS fail_closed_read ON gptbridge_index.fail_closed_action;
CREATE POLICY fail_closed_read ON gptbridge_index.fail_closed_action
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS fail_closed_write ON gptbridge_index.fail_closed_action;
CREATE POLICY fail_closed_write ON gptbridge_index.fail_closed_action
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.fail_closed_action FROM PUBLIC;
GRANT SELECT ON gptbridge_index.fail_closed_action TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.fail_closed_action
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS fail_closed_active_idx
    ON gptbridge_index.fail_closed_action (active, triggered_at);
CREATE INDEX IF NOT EXISTS fail_closed_domain_idx
    ON gptbridge_index.fail_closed_action (domain, active);

-- ============================================================================
-- trigger_fail_closed() — trigger a fail-closed action
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.trigger_fail_closed(
    p_trigger_type text,
    p_action_taken text,
    p_domain text,
    p_triggered_by text,
    p_trigger_entity_id text DEFAULT NULL,
    p_trigger_details jsonb DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.fail_closed_action (
        trigger_type, action_taken, domain, triggered_by,
        trigger_entity_id, trigger_details
    )
    VALUES (
        p_trigger_type, p_action_taken, p_domain, p_triggered_by,
        p_trigger_entity_id, p_trigger_details
    )
    RETURNING action_id INTO v_id;

    -- Also set the domain to read-only if applicable
    IF p_action_taken IN ('read_only', 'quarantine', 'block_writes') THEN
        PERFORM gptbridge_index.set_domain_readonly(p_domain, p_triggered_by);
    END IF;

    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- release_fail_closed() — release a fail-closed action
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.release_fail_closed(
    p_action_id uuid,
    p_released_by text,
    p_release_reason text
) RETURNS void AS $$
DECLARE
    v_domain text;
    v_action text;
BEGIN
    SELECT domain, action_taken INTO v_domain, v_action
    FROM gptbridge_index.fail_closed_action
    WHERE action_id = p_action_id;

    UPDATE gptbridge_index.fail_closed_action
    SET active = false,
        released_at = now(),
        released_by = p_released_by,
        release_reason = p_release_reason
    WHERE action_id = p_action_id;

    -- Clear read-only if this was the action
    IF v_action IN ('read_only', 'quarantine', 'block_writes') THEN
        PERFORM gptbridge_index.set_domain_readonly(v_domain, p_released_by);
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- is_fail_closed_active() — check if any fail-closed action is active
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.is_fail_closed_active(
    p_domain text DEFAULT NULL
) RETURNS boolean AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM gptbridge_index.fail_closed_action
        WHERE active = true
          AND (p_domain IS NULL OR domain = p_domain)
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_active_fail_closed() — get active fail-closed actions
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_active_fail_closed(
    p_domain text DEFAULT NULL
) RETURNS TABLE (
    action_id uuid,
    trigger_type text,
    action_taken text,
    domain text,
    triggered_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT action_id, trigger_type, action_taken, domain, triggered_at
    FROM gptbridge_index.fail_closed_action
    WHERE active = true
      AND (p_domain IS NULL OR domain = p_domain)
    ORDER BY triggered_at DESC;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
