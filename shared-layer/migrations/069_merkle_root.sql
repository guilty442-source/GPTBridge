-- 069_merkle_root.sql
-- Merkle Root.
--
-- For large datasets, don't verify row-by-row.  Build Merkle roots for:
--   - a batch of resources
--   - an audit partition
--   - an archive package
--   - a reconcile batch
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- merkle_root — Merkle root for a batch of data
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.merkle_root (
    merkle_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    domain text NOT NULL CHECK (domain IN (
        'resource_batch', 'audit_partition', 'archive_package',
        'reconcile_batch', 'sqlite_database', 'custom'
    )),
    domain_id text NOT NULL,  -- batch_id, partition_name, archive_id, etc.
    leaf_count integer NOT NULL,
    merkle_root text NOT NULL,
    leaf_hashes jsonb NOT NULL DEFAULT '[]'::jsonb,  -- ordered leaf hashes
    computed_at timestamptz NOT NULL DEFAULT now(),
    verified_at timestamptz,
    tamper_state text NOT NULL DEFAULT 'unverified' CHECK (tamper_state IN (
        'verified', 'unverified', 'mismatch', 'tampered',
        'incomplete', 'rebuild_required'
    ))
);

ALTER TABLE gptbridge_index.merkle_root ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.merkle_root FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS merkle_root_read ON gptbridge_index.merkle_root;
CREATE POLICY merkle_root_read ON gptbridge_index.merkle_root
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS merkle_root_write ON gptbridge_index.merkle_root;
CREATE POLICY merkle_root_write ON gptbridge_index.merkle_root
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.merkle_root FROM PUBLIC;
GRANT SELECT ON gptbridge_index.merkle_root TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.merkle_root
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS merkle_domain_idx
    ON gptbridge_index.merkle_root (domain, domain_id);
CREATE INDEX IF NOT EXISTS merkle_tamper_idx
    ON gptbridge_index.merkle_root (tamper_state, computed_at);

-- ============================================================================
-- compute_merkle_root() — compute Merkle root from leaf hashes
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.compute_merkle_root(
    p_leaf_hashes jsonb
) RETURNS text AS $$
DECLARE
    v_leaves text[];
    v_level text[];
    v_next text[];
    v_hash text;
    v_pair text;
    i integer;
BEGIN
    -- Extract leaf hashes from JSON array
    SELECT array_agg(value::text) INTO v_leaves
    FROM jsonb_array_elements(p_leaf_hashes);

    IF v_leaves IS NULL OR array_length(v_leaves, 1) = 0 THEN
        RETURN NULL;
    END IF;

    v_level := v_leaves;

    -- Build tree level by level
    WHILE array_length(v_level, 1) > 1 LOOP
        v_next := ARRAY[]::text[];
        i := 1;
        WHILE i <= array_length(v_level, 1) LOOP
            IF i + 1 <= array_length(v_level, 1) THEN
                v_pair := v_level[i] || v_level[i + 1];
                v_hash := encode(digest(v_pair, 'sha256'), 'hex');
                v_next := array_append(v_next, v_hash);
                i := i + 2;
            ELSE
                -- Odd node: promote to next level
                v_next := array_append(v_next, v_level[i]);
                i := i + 1;
            END IF;
        END LOOP;
        v_level := v_next;
    END LOOP;

    RETURN v_level[1];
END;
$$ LANGUAGE plpgsql IMMUTABLE SECURITY DEFINER SET search_path = pg_catalog, public;

-- ============================================================================
-- record_merkle_root() — record a Merkle root for a domain
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.record_merkle_root(
    p_domain text,
    p_domain_id text,
    p_leaf_hashes jsonb
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_root text;
    v_count integer;
BEGIN
    v_root := gptbridge_index.compute_merkle_root(p_leaf_hashes);
    v_count := jsonb_array_length(p_leaf_hashes);

    INSERT INTO gptbridge_index.merkle_root (
        domain, domain_id, leaf_count, merkle_root, leaf_hashes
    )
    VALUES (p_domain, p_domain_id, v_count, v_root, p_leaf_hashes)
    RETURNING merkle_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- verify_merkle_root() — verify a Merkle root matches expected
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.verify_merkle_root(
    p_merkle_id uuid,
    p_expected_root text
) RETURNS boolean AS $$
DECLARE
    v_actual text;
BEGIN
    SELECT merkle_root INTO v_actual
    FROM gptbridge_index.merkle_root
    WHERE merkle_id = p_merkle_id;

    IF v_actual = p_expected_root THEN
        UPDATE gptbridge_index.merkle_root
        SET tamper_state = 'verified', verified_at = now()
        WHERE merkle_id = p_merkle_id;
        RETURN true;
    ELSE
        UPDATE gptbridge_index.merkle_root
        SET tamper_state = 'mismatch'
        WHERE merkle_id = p_merkle_id;
        RETURN false;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_merkle_root_for_domain() — get latest Merkle root for a domain
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_merkle_root_for_domain(
    p_domain text,
    p_domain_id text
) RETURNS text AS $$
DECLARE
    v_root text;
BEGIN
    SELECT merkle_root INTO v_root
    FROM gptbridge_index.merkle_root
    WHERE domain = p_domain AND domain_id = p_domain_id
    ORDER BY computed_at DESC
    LIMIT 1;
    RETURN v_root;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
