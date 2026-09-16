-- 105_recovery_retry_policy.sql
-- Recovery Retry Policy.
--
-- Each stage has: max_attempts, timeout, backoff, escalation_policy.
-- No infinite retry.
--   PG reconnect 5 failures → DEGRADED
--   Reconcile record 3 failures → FAILED / conflict queue
--
-- Codex basis:
--   A10/E10 — explicit-allowlist.
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.recovery_retry_policy (
    policy_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    plan_id text REFERENCES gptbridge_index.recovery_plan(plan_id),
    step_name text NOT NULL,
    max_attempts integer NOT NULL DEFAULT 3,
    timeout_seconds integer NOT NULL DEFAULT 300,
    backoff_strategy text NOT NULL DEFAULT 'exponential' CHECK (backoff_strategy IN (
        'fixed', 'linear', 'exponential', 'none'
    )),
    initial_delay_seconds integer NOT NULL DEFAULT 1,
    max_delay_seconds integer NOT NULL DEFAULT 60,
    escalation_action text NOT NULL DEFAULT 'degrade' CHECK (escalation_action IN (
        'degrade', 'fail', 'quarantine', 'manual_intervention', 'abort'
    )),
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.recovery_retry_policy ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.recovery_retry_policy FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS recovery_retry_read ON gptbridge_index.recovery_retry_policy;
CREATE POLICY recovery_retry_read ON gptbridge_index.recovery_retry_policy
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS recovery_retry_write ON gptbridge_index.recovery_retry_policy;
CREATE POLICY recovery_retry_write ON gptbridge_index.recovery_retry_policy
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.recovery_retry_policy FROM PUBLIC;
GRANT SELECT ON gptbridge_index.recovery_retry_policy TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.recovery_retry_policy
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS recovery_retry_plan_idx
    ON gptbridge_index.recovery_retry_policy (plan_id, step_name);

CREATE OR REPLACE FUNCTION gptbridge_index.register_retry_policy(
    p_plan_id text,
    p_step_name text,
    p_max_attempts integer DEFAULT 3,
    p_timeout_seconds integer DEFAULT 300,
    p_backoff_strategy text DEFAULT 'exponential',
    p_initial_delay integer DEFAULT 1,
    p_max_delay integer DEFAULT 60,
    p_escalation_action text DEFAULT 'degrade'
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.recovery_retry_policy (
        plan_id, step_name, max_attempts, timeout_seconds,
        backoff_strategy, initial_delay_seconds, max_delay_seconds,
        escalation_action
    )
    VALUES (
        p_plan_id, p_step_name, p_max_attempts, p_timeout_seconds,
        p_backoff_strategy, p_initial_delay, p_max_delay,
        p_escalation_action
    )
    RETURNING policy_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.get_retry_policy(
    p_plan_id text,
    p_step_name text
) RETURNS TABLE (
    max_attempts integer, timeout_seconds integer,
    backoff_strategy text, escalation_action text
) AS $$
BEGIN
    RETURN QUERY
    SELECT max_attempts, timeout_seconds, backoff_strategy, escalation_action
    FROM gptbridge_index.recovery_retry_policy
    WHERE plan_id = p_plan_id AND step_name = p_step_name
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
