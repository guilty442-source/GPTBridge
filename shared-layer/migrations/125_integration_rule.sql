-- 125_integration_rule.sql
-- Integration Rules.
--
-- The 12 formal integration rules written into the architecture:
--   1. PostgreSQL is central structured authority.
--   2. SQLite must not become cross-module central authority.
--   3. Codex SQLite is exception: formal read-only codex authority.
--   4. Qdrant is canonical semantic index.
--   5. PostgreSQL does not store canonical vectors.
--   6. Qdrant does not replace PostgreSQL structured authority.
--   7. LOCAL-VECTOR is bounded fallback only.
--   8. All cross-engine operations use durable workflow/Saga.
--   9. All degradation must be bounded + observable + reconciled.
--  10. All recovery must complete verification before resume.
--  11. Derived read models must be rebuildable.
--  12. Runtime must not modify governance permissions or codex.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist.
--   A44/E30 — four-functions-local.
--   A46/E22 — Audit: mandatory-ledger.
--   A52/E38 — RAG: Qdrant canonical semantic index.

CREATE TABLE IF NOT EXISTS gptbridge_index.integration_rule (
    rule_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    rule_number integer NOT NULL UNIQUE,
    rule_text text NOT NULL,
    rule_category text NOT NULL CHECK (rule_category IN (
        'authority_boundary', 'engine_role', 'operational_constraint',
        'degradation_policy', 'recovery_policy', 'derived_state',
        'governance_constraint'
    )),
    enforced_by text NOT NULL DEFAULT 'runtime_contract',
    violation_effect text NOT NULL DEFAULT 'block' CHECK (violation_effect IN (
        'block', 'degrade', 'quarantine', 'warn', 'fail_closed'
    )),
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.integration_rule ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.integration_rule FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS integration_rule_read ON gptbridge_index.integration_rule;
CREATE POLICY integration_rule_read ON gptbridge_index.integration_rule
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS integration_rule_write ON gptbridge_index.integration_rule;
CREATE POLICY integration_rule_write ON gptbridge_index.integration_rule
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.integration_rule FROM PUBLIC;
GRANT SELECT ON gptbridge_index.integration_rule TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.integration_rule
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS integration_rule_number_idx
    ON gptbridge_index.integration_rule (rule_number, active);

-- Pre-populate the 12 integration rules
INSERT INTO gptbridge_index.integration_rule (
    rule_number, rule_text, rule_category, violation_effect
) VALUES
    (1, 'PostgreSQL is the central structured authority',
     'authority_boundary', 'block'),
    (2, 'SQLite must not become cross-module central authority',
     'engine_role', 'block'),
    (3, 'Codex SQLite is the exception: formal read-only codex authority',
     'authority_boundary', 'quarantine'),
    (4, 'Qdrant is the canonical semantic index',
     'engine_role', 'degrade'),
    (5, 'PostgreSQL does not store canonical vectors',
     'engine_role', 'block'),
    (6, 'Qdrant does not replace PostgreSQL structured authority',
     'engine_role', 'block'),
    (7, 'LOCAL-VECTOR is bounded fallback only',
     'operational_constraint', 'degrade'),
    (8, 'All cross-engine operations use durable workflow or Saga',
     'operational_constraint', 'block'),
    (9, 'All degradation must be bounded, observable, and reconciled',
     'degradation_policy', 'fail_closed'),
    (10, 'All recovery must complete verification before resume',
     'recovery_policy', 'block'),
    (11, 'Derived read models must be rebuildable',
     'derived_state', 'degrade'),
    (12, 'Runtime must not modify governance permissions or codex',
     'governance_constraint', 'fail_closed')
ON CONFLICT (rule_number) DO NOTHING;

CREATE OR REPLACE FUNCTION gptbridge_index.get_integration_rules()
RETURNS TABLE (
    rule_number integer, rule_text text,
    rule_category text, violation_effect text
) AS $$
BEGIN
    RETURN QUERY
    SELECT rule_number, rule_text, rule_category, violation_effect
    FROM gptbridge_index.integration_rule
    WHERE active = true
    ORDER BY rule_number;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.check_integration_rule(
    p_rule_number integer
) RETURNS TABLE (
    rule_text text, violation_effect text, active boolean
) AS $$
BEGIN
    RETURN QUERY
    SELECT rule_text, violation_effect, active
    FROM gptbridge_index.integration_rule
    WHERE rule_number = p_rule_number;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
