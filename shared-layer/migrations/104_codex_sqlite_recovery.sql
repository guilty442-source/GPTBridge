-- 102_codex_sqlite_recovery.sql
-- Codex SQLite Special Recovery.
--
-- governance_codex.sqlite3 is special: official read-only codex DB.
-- Cannot use normal "recreate empty DB" flow.
-- Flow: read failure → quarantine → verify known hash/signature →
-- restore certified codex copy → read-only verification → resume.
-- PostgreSQL cannot replace it.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A44/E30 — four-functions-local.

CREATE TABLE IF NOT EXISTS gptbridge_index.codex_sqlite_recovery (
    recovery_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    codex_db_path text NOT NULL,
    failure_type text NOT NULL CHECK (failure_type IN (
        'read_failure', 'hash_mismatch', 'signature_mismatch',
        'corruption_detected', 'missing_file'
    )),
    status text NOT NULL DEFAULT 'quarantined' CHECK (status IN (
        'quarantined', 'verifying_hash', 'restoring_certified',
        'read_only_verification', 'resumed', 'failed'
    )),
    known_hash text,
    actual_hash text,
    hash_verified boolean,
    restored_from_source text,  -- certified copy source locator
    restored_at timestamptz,
    verified_at timestamptz,
    resumed_at timestamptz,
    started_at timestamptz NOT NULL DEFAULT now(),
    notes text
);

ALTER TABLE gptbridge_index.codex_sqlite_recovery ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.codex_sqlite_recovery FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS codex_sqlite_rec_read ON gptbridge_index.codex_sqlite_recovery;
CREATE POLICY codex_sqlite_rec_read ON gptbridge_index.codex_sqlite_recovery
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS codex_sqlite_rec_write ON gptbridge_index.codex_sqlite_recovery;
CREATE POLICY codex_sqlite_rec_write ON gptbridge_index.codex_sqlite_recovery
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.codex_sqlite_recovery FROM PUBLIC;
GRANT SELECT ON gptbridge_index.codex_sqlite_recovery TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.codex_sqlite_recovery
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS codex_sqlite_rec_status_idx
    ON gptbridge_index.codex_sqlite_recovery (status, started_at);

CREATE OR REPLACE FUNCTION gptbridge_index.start_codex_recovery(
    p_incident_id uuid,
    p_codex_db_path text,
    p_failure_type text,
    p_known_hash text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.codex_sqlite_recovery (
        incident_id, codex_db_path, failure_type, known_hash
    )
    VALUES (p_incident_id, p_codex_db_path, p_failure_type, p_known_hash)
    RETURNING recovery_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.advance_codex_recovery(
    p_recovery_id uuid,
    p_new_status text,
    p_actual_hash text DEFAULT NULL,
    p_hash_verified boolean DEFAULT NULL,
    p_restored_from_source text DEFAULT NULL,
    p_notes text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.codex_sqlite_recovery
    SET status = p_new_status,
        actual_hash = COALESCE(p_actual_hash, actual_hash),
        hash_verified = COALESCE(p_hash_verified, hash_verified),
        restored_from_source = COALESCE(p_restored_from_source, restored_from_source),
        restored_at = CASE WHEN p_new_status IN ('read_only_verification', 'resumed') THEN COALESCE(restored_at, now()) ELSE restored_at END,
        verified_at = CASE WHEN p_new_status IN ('resumed') THEN COALESCE(verified_at, now()) ELSE verified_at END,
        resumed_at = CASE WHEN p_new_status = 'resumed' THEN now() ELSE resumed_at END,
        notes = COALESCE(p_notes, notes)
    WHERE recovery_id = p_recovery_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
