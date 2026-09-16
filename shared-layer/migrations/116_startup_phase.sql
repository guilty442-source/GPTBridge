-- 116_startup_phase.sql
-- Startup Phase Sequence.
--
-- PHASE 0: Bootstrap (config, directories, manifest)
-- PHASE 1: Governance/Identity (codex SQLite, identity, permission, security gen)
-- PHASE 2: Database Foundation (connection manager, pool, health — CONNECT not WRITE)
-- PHASE 3: Central Authority (PG certified → index/transport/audit/rag/identity ready)
-- PHASE 4: Module-private SQLite (per-class: path/schema/integrity/gen/WAL)
-- PHASE 5: Semantic Index (Qdrant: collections/contract/dimension/payload/embedding)
-- PHASE 6: Transport + Reconcile (recovery → lease → unknown commit → backlog → workers)
-- PHASE 7: Read Models / Cache (projection, cache, compact status, UI read model)
-- PHASE 8: READY (governance+security+authority+audit+manifest → CORE_READY)
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist.
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.startup_phase (
    phase_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    phase_number integer NOT NULL UNIQUE,
    phase_name text NOT NULL,
    description text,
    required_components text[] NOT NULL DEFAULT '{}',
    write_enabled boolean NOT NULL DEFAULT false,
    can_accept_requests boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now()
);

ALTER TABLE gptbridge_index.startup_phase ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.startup_phase FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS startup_phase_read ON gptbridge_index.startup_phase;
CREATE POLICY startup_phase_read ON gptbridge_index.startup_phase
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS startup_phase_write ON gptbridge_index.startup_phase;
CREATE POLICY startup_phase_write ON gptbridge_index.startup_phase
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.startup_phase FROM PUBLIC;
GRANT SELECT ON gptbridge_index.startup_phase TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.startup_phase
    TO gptbridge_index_executor;

-- Pre-populate the 9 startup phases
INSERT INTO gptbridge_index.startup_phase (
    phase_number, phase_name, description, required_components,
    write_enabled, can_accept_requests
) VALUES
    (0, 'BOOTSTRAP', 'Load config, resolve directories, verify runtime, load manifest',
     ARRAY['config', 'directories', 'runtime', 'manifest'], false, false),
    (1, 'GOVERNANCE_VALIDATED', 'Verify codex SQLite, identity, permission, security generation',
     ARRAY['governance_codex_sqlite', 'identity_directory', 'permission_config', 'security_generation'], false, false),
    (2, 'DATABASE_FOUNDATION_READY', 'Init Python DB layer: config, detection, connection, pool, health (CONNECT not WRITE)',
     ARRAY['connection_manager', 'pool_manager', 'health_check'], false, false),
    (3, 'CENTRAL_AUTHORITY_READY', 'PG certified → index/transport/audit/rag/identity schemas ready',
     ARRAY['postgresql', 'gptbridge_index', 'gptbridge_transport', 'gptbridge_audit', 'gptbridge_rag', 'gptbridge_identity'], true, false),
    (4, 'PRIVATE_STATE_READY', 'Load module-private/runtime/checkpoint/fallback SQLite per class',
     ARRAY['module_private_sqlite', 'runtime_checkpoint_sqlite', 'fallback_cache_sqlite'], true, false),
    (5, 'SEMANTIC_INDEX_READY', 'Qdrant: collections, contract, dimension, payload, embedding, generation',
     ARRAY['qdrant', 'rag_metadata'], true, false),
    (6, 'RECOVERY_READY', 'Transport recovery, lease recovery, unknown commit, reconcile backlog, start workers',
     ARRAY['transport', 'reconcile'], true, false),
    (7, 'READ_MODELS_READY', 'Projection, cache, compact status, UI read model (derived state last)',
     ARRAY['read_model', 'cache'], true, false),
    (8, 'CORE_READY', 'All gates passed: governance+security+authority+audit+manifest',
     ARRAY['governance_ready', 'security_ready', 'authority_ready', 'audit_ready', 'manifest_ready'], true, true)
ON CONFLICT (phase_number) DO NOTHING;

CREATE OR REPLACE FUNCTION gptbridge_index.get_startup_phase(
    p_phase_number integer
) RETURNS TABLE (
    phase_name text, required_components text[],
    write_enabled boolean, can_accept_requests boolean
) AS $$
BEGIN
    RETURN QUERY
    SELECT phase_name, required_components, write_enabled, can_accept_requests
    FROM gptbridge_index.startup_phase
    WHERE phase_number = p_phase_number;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.get_startup_order()
RETURNS TABLE (
    phase_number integer, phase_name text,
    required_components text[], write_enabled boolean
) AS $$
BEGIN
    RETURN QUERY
    SELECT phase_number, phase_name, required_components, write_enabled
    FROM gptbridge_index.startup_phase
    ORDER BY phase_number;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
