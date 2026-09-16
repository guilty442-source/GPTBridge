-- 040_database_release_manifest.sql
-- Database Release Manifest + State Machine.
--
-- Binds PostgreSQL schema, SQLite template, RLS, Role, Migration,
-- Qdrant collection, query contract, reconcile contract, backup format,
-- and minimum runtime version into a single Database Release.
--
-- State machine: DRAFT → VALIDATED → CERTIFIED → STAGED → ACTIVE
--                → SUPERSEDED → ARCHIVED  (failure: REJECTED)
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist; deny-by-default.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- database_release — the release manifest
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.database_release (
    release_id text PRIMARY KEY,  -- e.g. DB-2026.09.16-01
    schema_version integer NOT NULL,
    migration_head integer NOT NULL,
    rls_version integer NOT NULL,
    role_version integer NOT NULL,
    sqlite_template_version integer NOT NULL,
    reconcile_contract_version integer NOT NULL,
    qdrant_contract_version integer NOT NULL,
    query_contract_version integer NOT NULL,
    backup_format_version integer NOT NULL DEFAULT 1,
    minimum_runtime_version text NOT NULL,
    compatibility_range jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL DEFAULT 'DRAFT' CHECK (state IN (
        'DRAFT', 'VALIDATED', 'CERTIFIED', 'STAGED',
        'ACTIVE', 'SUPERSEDED', 'ARCHIVED', 'REJECTED'
    )),
    certification_result jsonb,
    previous_release_id text REFERENCES gptbridge_index.database_release(release_id),
    created_at timestamptz NOT NULL DEFAULT now(),
    activated_at timestamptz,
    superseded_at timestamptz,
    created_by text NOT NULL
);

ALTER TABLE gptbridge_index.database_release ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.database_release FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS db_release_read ON gptbridge_index.database_release;
CREATE POLICY db_release_read ON gptbridge_index.database_release
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS db_release_write ON gptbridge_index.database_release;
CREATE POLICY db_release_write ON gptbridge_index.database_release
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.database_release FROM PUBLIC;
GRANT SELECT ON gptbridge_index.database_release TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.database_release
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS db_release_state_idx
    ON gptbridge_index.database_release (state, created_at);
CREATE INDEX IF NOT EXISTS db_release_previous_idx
    ON gptbridge_index.database_release (previous_release_id);

-- ============================================================================
-- transition_release_state() — enforce the state machine
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.transition_release_state(
    p_release_id text,
    p_new_state text,
    p_transitioned_by text
) RETURNS void AS $$
DECLARE
    v_current text;
    v_valid_transitions jsonb;
    v_allowed text[];
BEGIN
    SELECT state INTO v_current
    FROM gptbridge_index.database_release
    WHERE release_id = p_release_id;

    IF v_current IS NULL THEN
        RAISE EXCEPTION 'Release % not found', p_release_id;
    END IF;

    -- Define valid transitions
    v_valid_transitions := jsonb_build_object(
        'DRAFT',      ARRAY['VALIDATED', 'REJECTED'],
        'VALIDATED',  ARRAY['CERTIFIED', 'REJECTED'],
        'CERTIFIED',  ARRAY['STAGED', 'REJECTED'],
        'STAGED',     ARRAY['ACTIVE', 'REJECTED'],
        'ACTIVE',     ARRAY['SUPERSEDED'],
        'SUPERSEDED', ARRAY['ARCHIVED'],
        'ARCHIVED',   ARRAY[]::text[]
    );

    v_allowed := v_valid_transitions->>v_current;
    IF v_allowed IS NULL OR NOT (p_new_state = ANY(v_allowed)) THEN
        RAISE EXCEPTION 'Invalid state transition: % -> %', v_current, p_new_state;
    END IF;

    UPDATE gptbridge_index.database_release
    SET state = p_new_state,
        activated_at = CASE WHEN p_new_state = 'ACTIVE' THEN now()
                            ELSE activated_at END,
        superseded_at = CASE WHEN p_new_state = 'SUPERSEDED' THEN now()
                             ELSE superseded_at END
    WHERE release_id = p_release_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_active_release() — return the currently ACTIVE release
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_active_release()
RETURNS TABLE (
    release_id text,
    schema_version integer,
    migration_head integer,
    rls_version integer,
    role_version integer,
    sqlite_template_version integer,
    qdrant_contract_version integer,
    query_contract_version integer,
    minimum_runtime_version text
) AS $$
BEGIN
    RETURN QUERY
    SELECT release_id, schema_version, migration_head,
           rls_version, role_version, sqlite_template_version,
           qdrant_contract_version, query_contract_version,
           minimum_runtime_version
    FROM gptbridge_index.database_release
    WHERE state = 'ACTIVE'
    ORDER BY activated_at DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- supersede_active_release() — mark the current ACTIVE release as SUPERSEDED
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.supersede_active_release(
    p_new_release_id text,
    p_superseded_by text
) RETURNS void AS $$
DECLARE
    v_old_id text;
BEGIN
    SELECT release_id INTO v_old_id
    FROM gptbridge_index.database_release
    WHERE state = 'ACTIVE'
    ORDER BY activated_at DESC
    LIMIT 1;

    IF v_old_id IS NOT NULL THEN
        PERFORM gptbridge_index.transition_release_state(
            v_old_id, 'SUPERSEDED', p_superseded_by
        );
    END IF;

    UPDATE gptbridge_index.database_release
    SET previous_release_id = v_old_id
    WHERE release_id = p_new_release_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
