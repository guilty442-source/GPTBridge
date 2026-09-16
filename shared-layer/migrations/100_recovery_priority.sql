-- 098_recovery_priority.sql
-- Recovery Priority.
--
-- Reconcile should not all have same priority:
--   P0 authority/security, P1 transport critical, P2 active resource state,
--   P3 RAG metadata, P4 historical/analytics
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist.

CREATE TABLE IF NOT EXISTS gptbridge_index.recovery_priority (
    priority_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    priority_class text NOT NULL CHECK (priority_class IN (
        'P0_authority_security', 'P1_transport_critical',
        'P2_active_resource', 'P3_rag_metadata', 'P4_historical_analytics'
    )),
    priority_level integer NOT NULL CHECK (priority_level BETWEEN 0 AND 4),
    description text,
    max_parallel integer NOT NULL DEFAULT 1,
    timeout_seconds integer NOT NULL DEFAULT 300,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.recovery_priority ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.recovery_priority FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS recovery_priority_read ON gptbridge_index.recovery_priority;
CREATE POLICY recovery_priority_read ON gptbridge_index.recovery_priority
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS recovery_priority_write ON gptbridge_index.recovery_priority;
CREATE POLICY recovery_priority_write ON gptbridge_index.recovery_priority
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.recovery_priority FROM PUBLIC;
GRANT SELECT ON gptbridge_index.recovery_priority TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.recovery_priority
    TO gptbridge_index_executor;

INSERT INTO gptbridge_index.recovery_priority (priority_class, priority_level, description, max_parallel, timeout_seconds) VALUES
    ('P0_authority_security', 0, 'Authority and security related state', 1, 60),
    ('P1_transport_critical', 1, 'Transport critical path', 2, 120),
    ('P2_active_resource', 2, 'Active resource state', 4, 300),
    ('P3_rag_metadata', 3, 'RAG metadata and index state', 2, 600),
    ('P4_historical_analytics', 4, 'Historical and analytics data', 1, 1800)
ON CONFLICT DO NOTHING;

CREATE OR REPLACE FUNCTION gptbridge_index.get_recovery_priority(
    p_priority_class text
) RETURNS TABLE (
    priority_level integer, max_parallel integer, timeout_seconds integer
) AS $$
BEGIN
    RETURN QUERY
    SELECT priority_level, max_parallel, timeout_seconds
    FROM gptbridge_index.recovery_priority
    WHERE priority_class = p_priority_class;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
