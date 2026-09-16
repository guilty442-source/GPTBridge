-- 085_release_signature.sql
-- Release Signature / Hash.
--
-- Each database dependency bundle gets a complete digest to prevent
-- "same version but different physical files".
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- release_signature — complete digest of a dependency bundle
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.release_signature (
    signature_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    release_id text REFERENCES gptbridge_index.database_release(release_id),
    bundle_hash text NOT NULL,  -- aggregate hash of all components
    component_count integer NOT NULL DEFAULT 0,
    component_hashes jsonb NOT NULL DEFAULT '[]'::jsonb,
    signature_algorithm text NOT NULL DEFAULT 'sha256',
    signed_by text NOT NULL,
    signed_at timestamptz NOT NULL DEFAULT now(),
    verified_at timestamptz,
    verified_by text,
    verification_result jsonb,
    tamper_state text NOT NULL DEFAULT 'unverified' CHECK (tamper_state IN (
        'verified', 'unverified', 'mismatch', 'tampered'
    ))
);

ALTER TABLE gptbridge_index.release_signature ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.release_signature FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS release_sig_read ON gptbridge_index.release_signature;
CREATE POLICY release_sig_read ON gptbridge_index.release_signature
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS release_sig_write ON gptbridge_index.release_signature;
CREATE POLICY release_sig_write ON gptbridge_index.release_signature
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.release_signature FROM PUBLIC;
GRANT SELECT ON gptbridge_index.release_signature TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.release_signature
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS release_sig_release_idx
    ON gptbridge_index.release_signature (release_id, signed_at);
CREATE INDEX IF NOT EXISTS release_sig_tamper_idx
    ON gptbridge_index.release_signature (tamper_state, signed_at);

-- ============================================================================
-- sign_release() — record a release signature (bundle_hash computed in Python)
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.sign_release(
    p_release_id text,
    p_bundle_hash text,
    p_component_hashes jsonb,
    p_signed_by text
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_count integer;
BEGIN
    v_count := COALESCE(jsonb_array_length(p_component_hashes), 0);

    INSERT INTO gptbridge_index.release_signature (
        release_id, bundle_hash, component_count,
        component_hashes, signed_by
    )
    VALUES (
        p_release_id, p_bundle_hash, v_count,
        p_component_hashes, p_signed_by
    )
    RETURNING signature_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- verify_release_signature() — verify a release signature
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.verify_release_signature(
    p_signature_id uuid,
    p_expected_bundle_hash text,
    p_verified_by text
) RETURNS boolean AS $$
DECLARE
    v_actual text;
BEGIN
    SELECT bundle_hash INTO v_actual
    FROM gptbridge_index.release_signature
    WHERE signature_id = p_signature_id;

    IF v_actual = p_expected_bundle_hash THEN
        UPDATE gptbridge_index.release_signature
        SET tamper_state = 'verified', verified_at = now(),
            verified_by = p_verified_by
        WHERE signature_id = p_signature_id;
        RETURN true;
    ELSE
        UPDATE gptbridge_index.release_signature
        SET tamper_state = 'mismatch'
        WHERE signature_id = p_signature_id;
        RETURN false;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_latest_signature() — get latest signature for a release
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_latest_signature(
    p_release_id text
) RETURNS TABLE (
    signature_id uuid,
    bundle_hash text,
    component_count integer,
    tamper_state text,
    signed_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT signature_id, bundle_hash, component_count,
           tamper_state, signed_at
    FROM gptbridge_index.release_signature
    WHERE release_id = p_release_id
    ORDER BY signed_at DESC
    LIMIT 1;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
