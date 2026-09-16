-- 065_reconcile_batch_digest.sql
-- Reconcile Batch Digest.
--
-- Each SQLite -> PostgreSQL reconciliation produces:
--   reconcile_run_id, source_generation, first_revision, last_revision,
--   record_count, batch_hash, result_hash
--
-- Not just "sync succeeded" but prove which batch was synced.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

-- ============================================================================
-- reconcile_batch_digest — per-batch reconciliation digest
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.reconcile_batch_digest (
    reconcile_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id text NOT NULL,
    source_generation integer NOT NULL,
    first_revision integer NOT NULL,
    last_revision integer NOT NULL,
    record_count integer NOT NULL,
    batch_hash text NOT NULL,     -- hash of the source batch
    result_hash text NOT NULL,   -- hash of the result after sync
    status text NOT NULL DEFAULT 'pending' CHECK (status IN (
        'pending', 'syncing', 'succeeded', 'failed', 'verified', 'mismatch'
    )),
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    verified_at timestamptz,
    failure_reason text,
    previous_run_id uuid REFERENCES gptbridge_index.reconcile_batch_digest(reconcile_run_id)
);

ALTER TABLE gptbridge_index.reconcile_batch_digest ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.reconcile_batch_digest FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS reconcile_digest_read ON gptbridge_index.reconcile_batch_digest;
CREATE POLICY reconcile_digest_read ON gptbridge_index.reconcile_batch_digest
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS reconcile_digest_write ON gptbridge_index.reconcile_batch_digest;
CREATE POLICY reconcile_digest_write ON gptbridge_index.reconcile_batch_digest
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.reconcile_batch_digest FROM PUBLIC;
GRANT SELECT ON gptbridge_index.reconcile_batch_digest TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.reconcile_batch_digest
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS reconcile_digest_module_idx
    ON gptbridge_index.reconcile_batch_digest (module_id, started_at);
CREATE INDEX IF NOT EXISTS reconcile_digest_status_idx
    ON gptbridge_index.reconcile_batch_digest (status, started_at);

-- ============================================================================
-- start_reconcile_batch() — start a reconciliation batch
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.start_reconcile_batch(
    p_module_id text,
    p_source_generation integer,
    p_first_revision integer,
    p_last_revision integer,
    p_batch_hash text
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
    v_prev uuid;
BEGIN
    SELECT reconcile_run_id INTO v_prev
    FROM gptbridge_index.reconcile_batch_digest
    WHERE module_id = p_module_id AND status = 'succeeded'
    ORDER BY completed_at DESC LIMIT 1;

    INSERT INTO gptbridge_index.reconcile_batch_digest (
        module_id, source_generation, first_revision, last_revision,
        batch_hash, status, previous_run_id
    )
    VALUES (
        p_module_id, p_source_generation, p_first_revision, p_last_revision,
        p_batch_hash, 'syncing', v_prev
    )
    RETURNING reconcile_run_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- complete_reconcile_batch() — complete a reconciliation batch
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.complete_reconcile_batch(
    p_reconcile_run_id uuid,
    p_succeeded boolean,
    p_record_count integer,
    p_result_hash text,
    p_failure_reason text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.reconcile_batch_digest
    SET status = CASE WHEN p_succeeded THEN 'succeeded' ELSE 'failed' END,
        record_count = p_record_count,
        result_hash = p_result_hash,
        completed_at = now(),
        failure_reason = p_failure_reason
    WHERE reconcile_run_id = p_reconcile_run_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- verify_reconcile_batch() — verify a batch digest matches
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.verify_reconcile_batch(
    p_reconcile_run_id uuid,
    p_expected_result_hash text
) RETURNS boolean AS $$
DECLARE
    v_actual text;
BEGIN
    SELECT result_hash INTO v_actual
    FROM gptbridge_index.reconcile_batch_digest
    WHERE reconcile_run_id = p_reconcile_run_id;

    IF v_actual = p_expected_result_hash THEN
        UPDATE gptbridge_index.reconcile_batch_digest
        SET status = 'verified', verified_at = now()
        WHERE reconcile_run_id = p_reconcile_run_id;
        RETURN true;
    ELSE
        UPDATE gptbridge_index.reconcile_batch_digest
        SET status = 'mismatch'
        WHERE reconcile_run_id = p_reconcile_run_id;
        RETURN false;
    END IF;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
