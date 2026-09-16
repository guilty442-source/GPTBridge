-- 028_rebuild_certification.sql
-- Rebuild Certification: after Qdrant rebuild, PostgreSQL restore, or
-- SQLite repair, the engine cannot go live until it passes count/hash/
-- revision/RLS/locator verification — not just SELECT 1.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A44/E30 — four-functions-local.
--   A52/E38 — RAG: Qdrant canonical semantic index.
--   A46/E22 — Audit: mandatory-ledger.
--
-- This migration adds:
--   * gptbridge_index.rebuild_certification — one row per rebuild attempt,
--     recording the engine, checks performed, and pass/fail.
--   * A function to record a certification result.

-- ============================================================================
-- rebuild_certification table
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.rebuild_certification (
    certification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    engine text NOT NULL CHECK (engine IN ('postgresql', 'qdrant', 'sqlite')),
    target text NOT NULL,  -- database name / collection / path
    rebuild_reason text NOT NULL,  -- 'restore', 'rebuild', 'repair', 'migration'
    checks_performed jsonb NOT NULL DEFAULT '[]'::jsonb,
    check_count integer NOT NULL DEFAULT 0,
    passed_count integer NOT NULL DEFAULT 0,
    certified boolean NOT NULL DEFAULT false,
    resource_count bigint,
    content_hash text,
    schema_version text,
    rls_verified boolean DEFAULT false,
    locator_verified boolean DEFAULT false,
    certified_at timestamptz NOT NULL DEFAULT now(),
    certified_by text NOT NULL
);

ALTER TABLE gptbridge_index.rebuild_certification ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.rebuild_certification FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS rebuild_cert_read ON gptbridge_index.rebuild_certification;
CREATE POLICY rebuild_cert_read ON gptbridge_index.rebuild_certification
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS rebuild_cert_write ON gptbridge_index.rebuild_certification;
CREATE POLICY rebuild_cert_write ON gptbridge_index.rebuild_certification
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.rebuild_certification FROM PUBLIC;
GRANT SELECT ON gptbridge_index.rebuild_certification TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.rebuild_certification
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS rebuild_cert_engine_idx
    ON gptbridge_index.rebuild_certification (engine, certified, certified_at);
CREATE INDEX IF NOT EXISTS rebuild_cert_target_idx
    ON gptbridge_index.rebuild_certification (target, certified_at);

-- ============================================================================
-- record_rebuild_certification() — called by the runtime certifier after
-- running all checks.  Returns the certification_id.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_rebuild_certification(
    p_engine text,
    p_target text,
    p_rebuild_reason text,
    p_checks jsonb,
    p_passed_count integer,
    p_check_count integer,
    p_resource_count bigint DEFAULT NULL,
    p_content_hash text DEFAULT NULL,
    p_schema_version text DEFAULT NULL,
    p_rls_verified boolean DEFAULT false,
    p_locator_verified boolean DEFAULT false,
    p_certified_by text
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_certified boolean;
BEGIN
    v_certified := p_passed_count = p_check_count
                   AND p_check_count > 0;
    INSERT INTO gptbridge_index.rebuild_certification (
        engine, target, rebuild_reason, checks_performed,
        check_count, passed_count, certified,
        resource_count, content_hash, schema_version,
        rls_verified, locator_verified, certified_by
    )
    VALUES (
        p_engine, p_target, p_rebuild_reason, p_checks,
        p_check_count, p_passed_count, v_certified,
        p_resource_count, p_content_hash, p_schema_version,
        p_rls_verified, p_locator_verified, p_certified_by
    )
    RETURNING certification_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- is_engine_certified() — check if an engine's latest rebuild is certified.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.is_engine_certified(
    p_engine text,
    p_target text
) RETURNS boolean AS $$
DECLARE
    v_certified boolean;
BEGIN
    SELECT certified INTO v_certified
    FROM gptbridge_index.rebuild_certification
    WHERE engine = p_engine AND target = p_target
    ORDER BY certified_at DESC
    LIMIT 1;
    RETURN COALESCE(v_certified, false);
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
