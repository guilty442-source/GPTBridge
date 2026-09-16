-- 047_canary_upgrade.sql
-- Canary Database Upgrade.
--
-- Before upgrading the production database:
--   production backup → temporary restore → apply new release →
--   full certification → success → upgrade production DB
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- canary_upgrade — tracks canary upgrade attempts
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.canary_upgrade (
    canary_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id text NOT NULL REFERENCES gptbridge_index.database_release(release_id),
    source_backup_id text,
    canary_database_name text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    status text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'restoring', 'migrating', 'certifying',
        'succeeded', 'failed', 'abandoned'
    )),
    certification_id uuid,  -- references rebuild_certification
    schema_hash text,
    failure_reason text,
    promoted_to_production boolean NOT NULL DEFAULT false,
    promoted_at timestamptz
);

ALTER TABLE gptbridge_index.canary_upgrade ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.canary_upgrade FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS canary_read ON gptbridge_index.canary_upgrade;
CREATE POLICY canary_read ON gptbridge_index.canary_upgrade
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS canary_write ON gptbridge_index.canary_upgrade;
CREATE POLICY canary_write ON gptbridge_index.canary_upgrade
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.canary_upgrade FROM PUBLIC;
GRANT SELECT ON gptbridge_index.canary_upgrade TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.canary_upgrade
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS canary_release_idx
    ON gptbridge_index.canary_upgrade (release_id, status);
CREATE INDEX IF NOT EXISTS canary_status_idx
    ON gptbridge_index.canary_upgrade (status, started_at);

-- ============================================================================
-- start_canary_upgrade() — start a canary upgrade
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.start_canary_upgrade(
    p_release_id text,
    p_canary_database_name text,
    p_source_backup_id text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.canary_upgrade (
        release_id, canary_database_name, source_backup_id, status
    )
    VALUES (p_release_id, p_canary_database_name, p_source_backup_id, 'restoring')
    RETURNING canary_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- complete_canary_upgrade() — mark a canary upgrade as succeeded or failed
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.complete_canary_upgrade(
    p_canary_id uuid,
    p_succeeded boolean,
    p_certification_id uuid DEFAULT NULL,
    p_schema_hash text DEFAULT NULL,
    p_failure_reason text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.canary_upgrade
    SET status = CASE WHEN p_succeeded THEN 'succeeded' ELSE 'failed' END,
        completed_at = now(),
        certification_id = p_certification_id,
        schema_hash = p_schema_hash,
        failure_reason = p_failure_reason
    WHERE canary_id = p_canary_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- promote_canary_to_production() — mark a canary as promoted
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.promote_canary_to_production(
    p_canary_id uuid
) RETURNS void AS $$
DECLARE
    v_status text;
BEGIN
    SELECT status INTO v_status
    FROM gptbridge_index.canary_upgrade
    WHERE canary_id = p_canary_id;

    IF v_status IS NULL THEN
        RAISE EXCEPTION 'Canary % not found', p_canary_id;
    END IF;

    IF v_status != 'succeeded' THEN
        RAISE EXCEPTION 'Canary % must be succeeded before promotion (current: %)',
            p_canary_id, v_status;
    END IF;

    UPDATE gptbridge_index.canary_upgrade
    SET promoted_to_production = true,
        promoted_at = now()
    WHERE canary_id = p_canary_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
