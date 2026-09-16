-- 107_recovery_idempotency.sql
-- Recovery Idempotency.
--
-- Every step must be idempotent:
--   create recovery incident, freeze fallback, restore backup,
--   enqueue reconcile, rebuild vectors
-- Re-running must not produce duplicate damage.
-- Recovery Orchestrator itself must survive crash/restart.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A10/E10 — explicit-allowlist.

CREATE TABLE IF NOT EXISTS gptbridge_index.recovery_idempotency (
    idempotency_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    recovery_run_id uuid NOT NULL,
    operation_key text NOT NULL,  -- e.g. 'freeze_fallback:module_x'
    operation_type text NOT NULL CHECK (operation_type IN (
        'create_incident', 'freeze_fallback', 'restore_backup',
        'enqueue_reconcile', 'rebuild_vectors', 'reclaim_lease',
        'switch_collection', 'promote_temp_instance'
    )),
    completed boolean NOT NULL DEFAULT false,
    completed_at timestamptz,
    result jsonb,
    attempts integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (recovery_run_id, operation_key)
);

ALTER TABLE gptbridge_index.recovery_idempotency ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.recovery_idempotency FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS recovery_idempotency_read ON gptbridge_index.recovery_idempotency;
CREATE POLICY recovery_idempotency_read ON gptbridge_index.recovery_idempotency
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS recovery_idempotency_write ON gptbridge_index.recovery_idempotency;
CREATE POLICY recovery_idempotency_write ON gptbridge_index.recovery_idempotency
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.recovery_idempotency FROM PUBLIC;
GRANT SELECT ON gptbridge_index.recovery_idempotency TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.recovery_idempotency
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS recovery_idempotency_run_idx
    ON gptbridge_index.recovery_idempotency (recovery_run_id, operation_key);

CREATE OR REPLACE FUNCTION gptbridge_index.check_or_mark_idempotent(
    p_recovery_run_id uuid,
    p_operation_key text,
    p_operation_type text
) RETURNS boolean AS $$
-- Returns true if operation was already completed (skip), false if newly marked
DECLARE
    v_exists boolean;
    v_completed boolean;
BEGIN
    SELECT completed INTO v_completed
    FROM gptbridge_index.recovery_idempotency
    WHERE recovery_run_id = p_recovery_run_id AND operation_key = p_operation_key;

    IF v_completed IS NOT NULL THEN
        RETURN v_completed;  -- if completed=true, skip; if false, can retry
    END IF;

    INSERT INTO gptbridge_index.recovery_idempotency (
        recovery_run_id, operation_key, operation_type, attempts
    )
    VALUES (
        p_recovery_run_id, p_operation_key, p_operation_type, 1
    )
    ON CONFLICT (recovery_run_id, operation_key) DO UPDATE SET
        attempts = recovery_idempotency.attempts + 1;

    RETURN false;  -- newly marked, proceed with operation
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.mark_idempotent_complete(
    p_recovery_run_id uuid,
    p_operation_key text,
    p_result jsonb DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.recovery_idempotency
    SET completed = true, completed_at = now(), result = p_result
    WHERE recovery_run_id = p_recovery_run_id AND operation_key = p_operation_key;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
