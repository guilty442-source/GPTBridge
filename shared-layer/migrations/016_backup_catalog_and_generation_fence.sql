-- 016_backup_catalog_and_generation_fence.sql
-- Backup Catalog + Generation Fence.
--
-- Backup Catalog: every backup records backup_id, engine, source_generation,
-- schema_version, hash, restore_tested_at — not just a file.
--
-- Generation Fence: every major restore/rebuild increments backend_generation.
-- Old connections holding stale generation are rejected from writing.
--
-- A44/E30 + A8/E21 + A46/E22.

-- ============================================================================
-- Backup catalog table
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.backup_catalog (
    backup_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    engine text NOT NULL CHECK (engine IN ('postgresql', 'sqlite', 'qdrant')),
    source_path text NOT NULL,
    backup_path text NOT NULL,
    source_generation bigint NOT NULL,
    schema_version text NOT NULL,
    backup_hash text NOT NULL,
    size_bytes bigint NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    restore_tested_at timestamptz,
    restore_certified boolean NOT NULL DEFAULT false,
    restore_certification jsonb NOT NULL DEFAULT '{}'::jsonb
);

ALTER TABLE gptbridge_index.backup_catalog ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.backup_catalog FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS backup_catalog_read ON gptbridge_index.backup_catalog;
CREATE POLICY backup_catalog_read ON gptbridge_index.backup_catalog
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS backup_catalog_write ON gptbridge_index.backup_catalog;
CREATE POLICY backup_catalog_write ON gptbridge_index.backup_catalog
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.backup_catalog FROM PUBLIC;
GRANT SELECT ON gptbridge_index.backup_catalog TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.backup_catalog TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS backup_catalog_engine_idx
    ON gptbridge_index.backup_catalog (engine, created_at);
CREATE INDEX IF NOT EXISTS backup_catalog_uncertified_idx
    ON gptbridge_index.backup_catalog (restore_certified)
    WHERE restore_certified = false;

-- ============================================================================
-- Generation fence: a global generation counter.
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.backend_generation_state (
    generation_id bigint PRIMARY KEY GENERATED ALWAYS AS IDENTITY,
    generation bigint NOT NULL UNIQUE,
    reason text NOT NULL,
    set_at timestamptz NOT NULL DEFAULT now(),
    set_by text NOT NULL
);

ALTER TABLE gptbridge_index.backend_generation_state ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.backend_generation_state FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS generation_state_read ON gptbridge_index.backend_generation_state;
CREATE POLICY generation_state_read ON gptbridge_index.backend_generation_state
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS generation_state_write ON gptbridge_index.backend_generation_state;
CREATE POLICY generation_state_write ON gptbridge_index.backend_generation_state
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.backend_generation_state FROM PUBLIC;
GRANT SELECT ON gptbridge_index.backend_generation_state TO gptbridge_index_reader;
GRANT SELECT, INSERT ON gptbridge_index.backend_generation_state TO gptbridge_index_executor;

-- Seed generation 1
INSERT INTO gptbridge_index.backend_generation_state (generation, reason, set_by)
VALUES (1, 'initial', 'bootstrap')
ON CONFLICT (generation) DO NOTHING;

-- ============================================================================
-- Current generation function (used by the fence check)
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.current_backend_generation()
RETURNS bigint LANGUAGE sql STABLE AS $$
    SELECT COALESCE(MAX(generation), 1) FROM gptbridge_index.backend_generation_state
$$;

-- ============================================================================
-- Generation fence trigger: reject writes from stale connections.
-- The application sets gptbridge.connection_generation via SET LOCAL before
-- each transaction.  If it's behind current_backend_generation, the write
-- is rejected.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.enforce_generation_fence()
RETURNS trigger AS $$
DECLARE
    conn_gen bigint;
    current_gen bigint;
BEGIN
    -- Read the connection's declared generation (NULL if not set)
    conn_gen := nullif(current_setting('gptbridge.connection_generation', true), '')::bigint;
    current_gen := gptbridge_index.current_backend_generation();
    -- If the connection declared a generation and it's stale, reject
    IF conn_gen IS NOT NULL AND conn_gen < current_gen THEN
        RAISE EXCEPTION 'GENERATION_FENCE: connection generation % is stale (current=%); reconnect or refresh',
            conn_gen, current_gen;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Apply the fence to resource writes
DROP TRIGGER IF EXISTS resource_generation_fence ON gptbridge_index.resource;
CREATE TRIGGER resource_generation_fence
    BEFORE INSERT OR UPDATE ON gptbridge_index.resource
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_index.enforce_generation_fence();

-- Apply the fence to transport writes
DROP TRIGGER IF EXISTS tool_request_generation_fence ON gptbridge_transport.tool_request;
CREATE TRIGGER tool_request_generation_fence
    BEFORE INSERT OR UPDATE ON gptbridge_transport.tool_request
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_index.enforce_generation_fence();

-- Function to bump the generation (called during restore/rebuild)
CREATE OR REPLACE FUNCTION gptbridge_index.bump_backend_generation(
    p_reason text,
    p_set_by text
) RETURNS bigint AS $$
DECLARE
    new_gen bigint;
BEGIN
    new_gen := gptbridge_index.current_backend_generation() + 1;
    INSERT INTO gptbridge_index.backend_generation_state (generation, reason, set_by)
    VALUES (new_gen, p_reason, p_set_by);
    RETURN new_gen;
END;
$$ LANGUAGE plpgsql;
