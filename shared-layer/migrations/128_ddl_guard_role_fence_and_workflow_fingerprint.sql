-- 128_ddl_guard_role_fence_and_workflow_fingerprint.sql
-- Fixes two verified drift points:
--   * 022 DDL guard trusted a session-set GUC alone (any session could
--     ``SET gptbridge.is_migration_executor = 'true'``) and 022 granted
--     gptbridge_migration_owner to the index executor — out-of-band DDL was
--     effectively permitted.
--   * 113 operation_fingerprint used concat_ws, which skips NULLs and does
--     not trim; Python ``workflow.fingerprint.operation_hash`` trims every
--     field and joins with explicit separators — the two sides disagreed.
--
-- Codex basis: A501/A502 (migration-executor-only, ordered hash-bound chain),
-- A445/A446 (Python/SQL parity for workflow fingerprints).

-- ============================================================================
-- 1. The index executor loses the migration-owner membership (022:96).
-- ============================================================================
REVOKE gptbridge_migration_owner FROM gptbridge_index_executor;

-- ============================================================================
-- 2. DDL guard: GUC declaration AND role membership are both required.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_security.ddl_guard()
RETURNS event_trigger AS $$
DECLARE
    v_declared boolean;
    v_role_ok boolean;
BEGIN
    v_declared := COALESCE(
        nullif(current_setting('gptbridge.is_migration_executor', true), ''),
        'false'
    )::boolean;

    v_role_ok := pg_has_role(current_user, 'gptbridge_migration_owner', 'MEMBER')
              OR pg_has_role(current_user, 'gptbridge_owner', 'MEMBER');

    IF NOT (v_declared AND v_role_ok) THEN
        RAISE EXCEPTION 'DDL_GUARD: DDL requires the migration executor declaration (gptbridge.is_migration_executor=true) AND migration-owner/owner role membership; out-of-band DDL is forbidden (A501/A502).';
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog;

-- ============================================================================
-- 3. Python/SQL fingerprint parity (A445): trim + explicit separators,
--    matching shared_layer.workflow.fingerprint.operation_hash exactly.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_workflow.operation_fingerprint(
    p_operation_type text,
    p_module_id text,
    p_resource_id text,
    p_revision text,
    p_payload_hash text
)
    RETURNS text
    LANGUAGE sql
    IMMUTABLE
AS $$
    SELECT encode(
        sha256(convert_to(
            btrim(COALESCE(p_operation_type, '')) || '|' ||
            btrim(COALESCE(p_module_id, '')) || '|' ||
            btrim(COALESCE(p_resource_id, '')) || '|' ||
            btrim(COALESCE(p_revision, '')) || '|' ||
            btrim(COALESCE(p_payload_hash, '')),
            'UTF8')),
        'hex');
$$;
