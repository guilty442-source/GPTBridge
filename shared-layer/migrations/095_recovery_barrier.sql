-- 093_recovery_barrier.sql
-- Recovery Barrier.
--
-- PG online BUT reconcile incomplete → RECOVERING_READ_ONLY
-- Not HEALTHY until: critical reconcile complete + authority conflicts resolved + integrity PASS
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist.

CREATE TABLE IF NOT EXISTS gptbridge_index.recovery_barrier (
    barrier_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    barrier_type text NOT NULL CHECK (barrier_type IN (
        'RECOVERING_READ_ONLY', 'RECONCILE_INCOMPLETE',
        'AUTHORITY_CONFLICT', 'INTEGRITY_PENDING'
    )),
    raised_at timestamptz NOT NULL DEFAULT now(),
    raised_by text NOT NULL,
    reason text,
    critical_reconcile_complete boolean NOT NULL DEFAULT false,
    authority_conflicts_resolved boolean NOT NULL DEFAULT false,
    integrity_pass boolean NOT NULL DEFAULT false,
    barrier_active boolean NOT NULL DEFAULT true,
    released_at timestamptz,
    released_by text,
    release_reason text
);

ALTER TABLE gptbridge_index.recovery_barrier ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.recovery_barrier FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS recovery_barrier_read ON gptbridge_index.recovery_barrier;
CREATE POLICY recovery_barrier_read ON gptbridge_index.recovery_barrier
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS recovery_barrier_write ON gptbridge_index.recovery_barrier;
CREATE POLICY recovery_barrier_write ON gptbridge_index.recovery_barrier
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.recovery_barrier FROM PUBLIC;
GRANT SELECT ON gptbridge_index.recovery_barrier TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.recovery_barrier
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS recovery_barrier_active_idx
    ON gptbridge_index.recovery_barrier (barrier_active, raised_at);

CREATE OR REPLACE FUNCTION gptbridge_index.raise_recovery_barrier(
    p_incident_id uuid,
    p_barrier_type text,
    p_raised_by text,
    p_reason text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.recovery_barrier (
        incident_id, barrier_type, raised_by, reason
    )
    VALUES (p_incident_id, p_barrier_type, p_raised_by, p_reason)
    RETURNING barrier_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.update_barrier_progress(
    p_barrier_id uuid,
    p_reconcile_complete boolean DEFAULT NULL,
    p_authority_resolved boolean DEFAULT NULL,
    p_integrity_pass boolean DEFAULT NULL
) RETURNS boolean AS $$
DECLARE
    v_all_pass boolean;
BEGIN
    UPDATE gptbridge_index.recovery_barrier
    SET critical_reconcile_complete = COALESCE(p_reconcile_complete, critical_reconcile_complete),
        authority_conflicts_resolved = COALESCE(p_authority_resolved, authority_conflicts_resolved),
        integrity_pass = COALESCE(p_integrity_pass, integrity_pass)
    WHERE barrier_id = p_barrier_id;

    SELECT critical_reconcile_complete AND authority_conflicts_resolved AND integrity_pass
    INTO v_all_pass
    FROM gptbridge_index.recovery_barrier
    WHERE barrier_id = p_barrier_id;

    RETURN COALESCE(v_all_pass, false);
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.release_recovery_barrier(
    p_barrier_id uuid,
    p_released_by text,
    p_release_reason text
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.recovery_barrier
    SET barrier_active = false, released_at = now(),
        released_by = p_released_by, release_reason = p_release_reason
    WHERE barrier_id = p_barrier_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.is_recovery_barrier_active()
RETURNS boolean AS $$
BEGIN
    RETURN EXISTS (
        SELECT 1 FROM gptbridge_index.recovery_barrier
        WHERE barrier_active = true
    );
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
