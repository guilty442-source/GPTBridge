-- 071_restore_verification.sql
-- Restore Verification.
--
-- After restore, not just "DB can open", but compare:
--   schema hash, audit head, resource Merkle root, generation, migration head
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- restore_verification — records restore verification results
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.restore_verification (
    verification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_id uuid REFERENCES gptbridge_index.integrity_snapshot(snapshot_id),
    restored_at timestamptz NOT NULL DEFAULT now(),
    restored_by text NOT NULL,
    target_database text NOT NULL,

    -- Expected values (from snapshot)
    expected_schema_hash text,
    expected_audit_head_hash text,
    expected_resource_merkle_root text,
    expected_generation integer,
    expected_migration_head integer,

    -- Actual values (measured after restore)
    actual_schema_hash text,
    actual_audit_head_hash text,
    actual_resource_merkle_root text,
    actual_generation integer,
    actual_migration_head integer,

    -- Comparison results
    schema_match boolean,
    audit_match boolean,
    merkle_match boolean,
    generation_match boolean,
    migration_match boolean,

    overall_passed boolean NOT NULL DEFAULT false,
    failure_reason text,
    verified_at timestamptz
);

ALTER TABLE gptbridge_index.restore_verification ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.restore_verification FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS restore_verify_read ON gptbridge_index.restore_verification;
CREATE POLICY restore_verify_read ON gptbridge_index.restore_verification
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS restore_verify_write ON gptbridge_index.restore_verification;
CREATE POLICY restore_verify_write ON gptbridge_index.restore_verification
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.restore_verification FROM PUBLIC;
GRANT SELECT ON gptbridge_index.restore_verification TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.restore_verification
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS restore_verify_date_idx
    ON gptbridge_index.restore_verification (restored_at);
CREATE INDEX IF NOT EXISTS restore_verify_passed_idx
    ON gptbridge_index.restore_verification (overall_passed, restored_at);

-- ============================================================================
-- record_restore_verification() — record a restore verification
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_restore_verification(
    p_snapshot_id uuid,
    p_restored_by text,
    p_target_database text,
    p_expected_schema_hash text,
    p_actual_schema_hash text,
    p_expected_audit_head_hash text DEFAULT NULL,
    p_actual_audit_head_hash text DEFAULT NULL,
    p_expected_resource_merkle_root text DEFAULT NULL,
    p_actual_resource_merkle_root text DEFAULT NULL,
    p_expected_generation integer DEFAULT NULL,
    p_actual_generation integer DEFAULT NULL,
    p_expected_migration_head integer DEFAULT NULL,
    p_actual_migration_head integer DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_schema_match boolean;
    v_audit_match boolean;
    v_merkle_match boolean;
    v_gen_match boolean;
    v_mig_match boolean;
    v_overall boolean;
    v_reasons text[] := ARRAY[]::text[];
BEGIN
    v_schema_match := (p_expected_schema_hash = p_actual_schema_hash);
    v_audit_match := (p_expected_audit_head_hash IS NULL
                      OR p_actual_audit_head_hash IS NULL
                      OR p_expected_audit_head_hash = p_actual_audit_head_hash);
    v_merkle_match := (p_expected_resource_merkle_root IS NULL
                       OR p_actual_resource_merkle_root IS NULL
                       OR p_expected_resource_merkle_root = p_actual_resource_merkle_root);
    v_gen_match := (p_expected_generation IS NULL
                    OR p_actual_generation IS NULL
                    OR p_expected_generation = p_actual_generation);
    v_mig_match := (p_expected_migration_head IS NULL
                    OR p_actual_migration_head IS NULL
                    OR p_expected_migration_head = p_actual_migration_head);

    IF NOT v_schema_match THEN
        v_reasons := array_append(v_reasons, 'schema_hash_mismatch');
    END IF;
    IF NOT v_audit_match THEN
        v_reasons := array_append(v_reasons, 'audit_head_mismatch');
    END IF;
    IF NOT v_merkle_match THEN
        v_reasons := array_append(v_reasons, 'merkle_root_mismatch');
    END IF;
    IF NOT v_gen_match THEN
        v_reasons := array_append(v_reasons, 'generation_mismatch');
    END IF;
    IF NOT v_mig_match THEN
        v_reasons := array_append(v_reasons, 'migration_head_mismatch');
    END IF;

    v_overall := v_schema_match AND v_audit_match AND v_merkle_match
                AND v_gen_match AND v_mig_match;

    INSERT INTO gptbridge_index.restore_verification (
        snapshot_id, restored_by, target_database,
        expected_schema_hash, actual_schema_hash,
        expected_audit_head_hash, actual_audit_head_hash,
        expected_resource_merkle_root, actual_resource_merkle_root,
        expected_generation, actual_generation,
        expected_migration_head, actual_migration_head,
        schema_match, audit_match, merkle_match,
        generation_match, migration_match,
        overall_passed, failure_reason, verified_at
    )
    VALUES (
        p_snapshot_id, p_restored_by, p_target_database,
        p_expected_schema_hash, p_actual_schema_hash,
        p_expected_audit_head_hash, p_actual_audit_head_hash,
        p_expected_resource_merkle_root, p_actual_resource_merkle_root,
        p_expected_generation, p_actual_generation,
        p_expected_migration_head, p_actual_migration_head,
        v_schema_match, v_audit_match, v_merkle_match,
        v_gen_match, v_mig_match,
        v_overall,
        CASE WHEN array_length(v_reasons, 1) IS NOT NULL
             THEN array_to_string(v_reasons, ', ')
        END,
        now()
    )
    RETURNING verification_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_failed_restores() — find restores that failed verification
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_failed_restores(
    p_limit integer DEFAULT 50
) RETURNS TABLE (
    verification_id uuid,
    target_database text,
    failure_reason text,
    restored_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT verification_id, target_database, failure_reason, restored_at
    FROM gptbridge_index.restore_verification
    WHERE overall_passed = false
    ORDER BY restored_at DESC
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
