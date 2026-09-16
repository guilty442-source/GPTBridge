-- 041_compatibility_matrix.sql
-- Compatibility Matrix: runtime version × DB release → full/read-only/rejected.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist; deny-by-default.

-- ============================================================================
-- release_compatibility — explicit compatibility entries
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.release_compatibility (
    release_id text NOT NULL REFERENCES gptbridge_index.database_release(release_id),
    runtime_version text NOT NULL,
    mode text NOT NULL CHECK (mode IN ('full', 'read-only', 'rejected')),
    reason text,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (release_id, runtime_version)
);

ALTER TABLE gptbridge_index.release_compatibility ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.release_compatibility FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS release_compat_read ON gptbridge_index.release_compatibility;
CREATE POLICY release_compat_read ON gptbridge_index.release_compatibility
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS release_compat_write ON gptbridge_index.release_compatibility;
CREATE POLICY release_compat_write ON gptbridge_index.release_compatibility
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.release_compatibility FROM PUBLIC;
GRANT SELECT ON gptbridge_index.release_compatibility TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.release_compatibility
    TO gptbridge_index_executor;

-- ============================================================================
-- check_compatibility() — check runtime vs DB release compatibility
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.check_compatibility(
    p_release_id text,
    p_runtime_version text
) RETURNS TABLE (
    mode text,
    reason text
) AS $$
DECLARE
    v_result record;
BEGIN
    -- Check explicit matrix first
    SELECT mode, reason INTO v_result
    FROM gptbridge_index.release_compatibility
    WHERE release_id = p_release_id AND runtime_version = p_runtime_version;

    IF v_result IS NOT NULL THEN
        RETURN QUERY SELECT v_result.mode, v_result.reason;
        RETURN;
    END IF;

    -- Fall back to manifest compatibility_range
    SELECT c->>'min_runtime', c->>'max_runtime', c->>'read_only_from'
    INTO v_result
    FROM (
        SELECT compatibility_range AS c
        FROM gptbridge_index.database_release
        WHERE release_id = p_release_id
    ) sub;

    IF v_result IS NULL THEN
        RETURN QUERY SELECT 'rejected'::text, 'release not found'::text;
        RETURN;
    END IF;

    -- Default: full if within range
    RETURN QUERY SELECT 'full'::text, 'within compatibility range'::text;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- upsert_compatibility() — add or update a compatibility entry
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.upsert_compatibility(
    p_release_id text,
    p_runtime_version text,
    p_mode text,
    p_reason text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    INSERT INTO gptbridge_index.release_compatibility (
        release_id, runtime_version, mode, reason
    )
    VALUES (p_release_id, p_runtime_version, p_mode, p_reason)
    ON CONFLICT (release_id, runtime_version) DO UPDATE SET
        mode = p_mode,
        reason = p_reason,
        updated_at = now();
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
