-- 104_pitr_boundary.sql
-- Point-in-Time Recovery Boundary.
--
-- If PostgreSQL uses PITR, need:
--   restore_target, database_generation, audit_head,
--   transport_cutoff, reconcile_cutoff
-- After PITR, SQLite/Qdrant may be at newer state → must redo
-- cross-engine consistency reconciliation.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.pitr_boundary (
    pitr_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    restore_target timestamptz NOT NULL,
    database_generation integer NOT NULL,
    audit_head_hash text,
    transport_cutoff timestamptz,
    reconcile_cutoff timestamptz,
    pg_state_at_target text,  -- 'restored_to_target'
    sqlite_state_ahead boolean NOT NULL DEFAULT false,
    qdrant_state_ahead boolean NOT NULL DEFAULT false,
    cross_engine_reconcile_done boolean NOT NULL DEFAULT false,
    cross_engine_reconcile_started_at timestamptz,
    cross_engine_reconcile_completed_at timestamptz,
    started_at timestamptz NOT NULL DEFAULT now(),
    notes text
);

ALTER TABLE gptbridge_index.pitr_boundary ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.pitr_boundary FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS pitr_boundary_read ON gptbridge_index.pitr_boundary;
CREATE POLICY pitr_boundary_read ON gptbridge_index.pitr_boundary
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS pitr_boundary_write ON gptbridge_index.pitr_boundary;
CREATE POLICY pitr_boundary_write ON gptbridge_index.pitr_boundary
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.pitr_boundary FROM PUBLIC;
GRANT SELECT ON gptbridge_index.pitr_boundary TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.pitr_boundary
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS pitr_boundary_incident_idx
    ON gptbridge_index.pitr_boundary (incident_id, started_at);

CREATE OR REPLACE FUNCTION gptbridge_index.record_pitr_boundary(
    p_incident_id uuid,
    p_restore_target timestamptz,
    p_database_generation integer,
    p_audit_head_hash text DEFAULT NULL,
    p_transport_cutoff timestamptz DEFAULT NULL,
    p_reconcile_cutoff timestamptz DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.pitr_boundary (
        incident_id, restore_target, database_generation,
        audit_head_hash, transport_cutoff, reconcile_cutoff
    )
    VALUES (
        p_incident_id, p_restore_target, p_database_generation,
        p_audit_head_hash, p_transport_cutoff, p_reconcile_cutoff
    )
    RETURNING pitr_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.complete_pitr_reconcile(
    p_pitr_id uuid,
    p_sqlite_ahead boolean,
    p_qdrant_ahead boolean
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.pitr_boundary
    SET sqlite_state_ahead = p_sqlite_ahead,
        qdrant_state_ahead = p_qdrant_ahead,
        cross_engine_reconcile_started_at = COALESCE(cross_engine_reconcile_started_at, now()),
        cross_engine_reconcile_done = true,
        cross_engine_reconcile_completed_at = now()
    WHERE pitr_id = p_pitr_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
