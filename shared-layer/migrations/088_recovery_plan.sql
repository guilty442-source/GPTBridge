-- 086_recovery_plan.sql
-- Recovery Plan Versioning.
--
-- Each incident type has a formal, versioned recovery plan:
--   recovery_plan_id, version, incident_type, preconditions, steps,
--   timeouts, rollback_strategy, verification_rules, required_authority
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- recovery_plan — versioned recovery plans
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.recovery_plan (
    plan_id text PRIMARY KEY,  -- e.g. 'pg_primary_unavailable'
    version integer NOT NULL DEFAULT 1,
    incident_type text NOT NULL,  -- 'pg_primary_unavailable', 'sqlite_fallback',
                                  -- 'qdrant_rebuild', 'backup_restore',
                                  -- 'schema_drift_quarantine'
    full_plan_id text GENERATED ALWAYS AS (plan_id || '_v' || version) STORED,
    preconditions jsonb NOT NULL DEFAULT '[]'::jsonb,
    steps jsonb NOT NULL DEFAULT '[]'::jsonb,  -- ordered list of recovery steps
    timeouts jsonb NOT NULL DEFAULT '{}'::jsonb,  -- per-step timeouts
    rollback_strategy text,
    verification_rules jsonb NOT NULL DEFAULT '[]'::jsonb,
    required_authority text NOT NULL DEFAULT 'governance_rule',
    status text NOT NULL DEFAULT 'draft' CHECK (status IN (
        'draft', 'validated', 'certified', 'active', 'superseded', 'retired'
    )),
    created_at timestamptz NOT NULL DEFAULT now(),
    certified_at timestamptz,
    certified_by text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.recovery_plan ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.recovery_plan FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS recovery_plan_read ON gptbridge_index.recovery_plan;
CREATE POLICY recovery_plan_read ON gptbridge_index.recovery_plan
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS recovery_plan_write ON gptbridge_index.recovery_plan;
CREATE POLICY recovery_plan_write ON gptbridge_index.recovery_plan
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.recovery_plan FROM PUBLIC;
GRANT SELECT ON gptbridge_index.recovery_plan TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.recovery_plan
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS recovery_plan_incident_idx
    ON gptbridge_index.recovery_plan (incident_type, status);
CREATE INDEX IF NOT EXISTS recovery_plan_status_idx
    ON gptbridge_index.recovery_plan (status, updated_at);

-- ============================================================================
-- register_recovery_plan() — register a new recovery plan version
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.register_recovery_plan(
    p_plan_id text,
    p_version integer,
    p_incident_type text,
    p_steps jsonb,
    p_preconditions jsonb DEFAULT '[]'::jsonb,
    p_timeouts jsonb DEFAULT '{}'::jsonb,
    p_rollback_strategy text DEFAULT NULL,
    p_verification_rules jsonb DEFAULT '[]'::jsonb,
    p_required_authority text DEFAULT 'governance_rule'
) RETURNS void AS $$
BEGIN
    INSERT INTO gptbridge_index.recovery_plan (
        plan_id, version, incident_type, steps, preconditions,
        timeouts, rollback_strategy, verification_rules, required_authority
    )
    VALUES (
        p_plan_id, p_version, p_incident_type, p_steps, p_preconditions,
        p_timeouts, p_rollback_strategy, p_verification_rules, p_required_authority
    )
    ON CONFLICT (plan_id) DO UPDATE SET
        version = EXCLUDED.version,
        incident_type = EXCLUDED.incident_type,
        steps = EXCLUDED.steps,
        preconditions = EXCLUDED.preconditions,
        timeouts = EXCLUDED.timeouts,
        rollback_strategy = EXCLUDED.rollback_strategy,
        verification_rules = EXCLUDED.verification_rules,
        required_authority = EXCLUDED.required_authority,
        status = 'draft',
        updated_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- certify_recovery_plan() — certify a recovery plan
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.certify_recovery_plan(
    p_plan_id text,
    p_certified_by text
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.recovery_plan
    SET status = 'certified', certified_at = now(), certified_by = p_certified_by,
        updated_at = now()
    WHERE plan_id = p_plan_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- activate_recovery_plan() — activate a certified plan (supersedes previous)
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.activate_recovery_plan(
    p_plan_id text
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.recovery_plan
    SET status = 'superseded', updated_at = now()
    WHERE plan_id != p_plan_id
      AND incident_type = (SELECT incident_type FROM gptbridge_index.recovery_plan WHERE plan_id = p_plan_id)
      AND status = 'active';

    UPDATE gptbridge_index.recovery_plan
    SET status = 'active', updated_at = now()
    WHERE plan_id = p_plan_id AND status = 'certified';
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_active_recovery_plan() — get the active plan for an incident type
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_active_recovery_plan(
    p_incident_type text
) RETURNS TABLE (
    plan_id text,
    version integer,
    steps jsonb,
    verification_rules jsonb,
    required_authority text
) AS $$
BEGIN
    RETURN QUERY
    SELECT plan_id, version, steps, verification_rules, required_authority
    FROM gptbridge_index.recovery_plan
    WHERE incident_type = p_incident_type AND status = 'active'
    ORDER BY version DESC LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
