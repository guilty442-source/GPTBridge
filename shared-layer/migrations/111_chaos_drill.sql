-- 109_chaos_drill.sql
-- Chaos Drill for Recovery Workflow.
--
-- Scenarios A-H to validate full recovery orchestration:
--   A: PostgreSQL offline 10 minutes
--   B: PG disconnect after uncertain commit
--   C: SQLite fallback accumulating 10000 writes
--   D: PG returns while reconcile running
--   E: Qdrant completely lost
--   F: SQLite WAL corruption
--   G: restore backup one release behind
--   H: process crashes halfway through recovery
--
-- Each confirms: no authority inversion, no duplicate write,
--   no lost committed operation, no silent conflict, no uncontrolled retry
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A10/E10 — explicit-allowlist.

CREATE TABLE IF NOT EXISTS gptbridge_index.chaos_drill (
    drill_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    scenario_code text NOT NULL CHECK (scenario_code IN (
        'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'
    )),
    scenario_name text NOT NULL,
    description text,
    plan_id text REFERENCES gptbridge_index.recovery_plan(plan_id),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'running', 'passed', 'failed', 'aborted'
    )),
    -- Verification gates
    no_authority_inversion boolean NOT NULL DEFAULT false,
    no_duplicate_write boolean NOT NULL DEFAULT false,
    no_lost_commit boolean NOT NULL DEFAULT false,
    no_silent_conflict boolean NOT NULL DEFAULT false,
    no_uncontrolled_retry boolean NOT NULL DEFAULT false,
    overall_passed boolean NOT NULL DEFAULT false,
    failure_reason text,
    drill_log jsonb DEFAULT '[]'::jsonb
);

ALTER TABLE gptbridge_index.chaos_drill ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.chaos_drill FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS chaos_drill_read ON gptbridge_index.chaos_drill;
CREATE POLICY chaos_drill_read ON gptbridge_index.chaos_drill
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS chaos_drill_write ON gptbridge_index.chaos_drill;
CREATE POLICY chaos_drill_write ON gptbridge_index.chaos_drill
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.chaos_drill FROM PUBLIC;
GRANT SELECT ON gptbridge_index.chaos_drill TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.chaos_drill
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS chaos_drill_status_idx
    ON gptbridge_index.chaos_drill (status, started_at);
CREATE INDEX IF NOT EXISTS chaos_drill_scenario_idx
    ON gptbridge_index.chaos_drill (scenario_code, started_at);

-- Pre-populate scenario definitions
INSERT INTO gptbridge_index.chaos_drill (scenario_code, scenario_name, description, status) VALUES
    ('A', 'PostgreSQL offline 10 minutes', 'PG goes offline for 10 minutes, verify SQLite fallback and recovery', 'pending'),
    ('B', 'PG disconnect after uncertain commit', 'PG disconnects after client sends INSERT, verify commit state resolution', 'pending'),
    ('C', 'SQLite fallback accumulating 10000 writes', 'PG offline, 10000 writes to SQLite fallback, verify reconcile', 'pending'),
    ('D', 'PG returns while reconcile running', 'PG comes back during reconcile, verify barrier and no data loss', 'pending'),
    ('E', 'Qdrant completely lost', 'Qdrant collection deleted, verify full rebuild from PG metadata', 'pending'),
    ('F', 'SQLite WAL corruption', 'SQLite WAL corrupted, verify WAL recovery and integrity check', 'pending'),
    ('G', 'Restore backup one release behind', 'Restore backup from previous release, verify schema migration and reconcile', 'pending'),
    ('H', 'Process crashes halfway through recovery', 'Recovery process crashes mid-recovery, verify checkpoint resume', 'pending')
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION gptbridge_index.start_chaos_drill(
    p_scenario_code text,
    p_plan_id text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.chaos_drill (scenario_code, scenario_name, description, plan_id, status)
    SELECT scenario_code, scenario_name, description, p_plan_id, 'running'
    FROM gptbridge_index.chaos_drill
    WHERE scenario_code = p_scenario_code AND status = 'pending'
    ORDER BY started_at DESC LIMIT 1
    RETURNING drill_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.complete_chaos_drill(
    p_drill_id uuid,
    p_no_authority_inversion boolean,
    p_no_duplicate_write boolean,
    p_no_lost_commit boolean,
    p_no_silent_conflict boolean,
    p_no_uncontrolled_retry boolean,
    p_failure_reason text DEFAULT NULL
) RETURNS void AS $$
DECLARE
    v_passed boolean;
BEGIN
    v_passed := p_no_authority_inversion AND p_no_duplicate_write AND
                p_no_lost_commit AND p_no_silent_conflict AND
                p_no_uncontrolled_retry;

    UPDATE gptbridge_index.chaos_drill
    SET status = CASE WHEN v_passed THEN 'passed' ELSE 'failed' END,
        no_authority_inversion = p_no_authority_inversion,
        no_duplicate_write = p_no_duplicate_write,
        no_lost_commit = p_no_lost_commit,
        no_silent_conflict = p_no_silent_conflict,
        no_uncontrolled_retry = p_no_uncontrolled_retry,
        overall_passed = v_passed,
        completed_at = now(),
        failure_reason = p_failure_reason
    WHERE drill_id = p_drill_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
