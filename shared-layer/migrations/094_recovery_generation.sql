-- 092_recovery_generation.sql
-- Recovery Generation.
--
-- Each incident creates a new recovery_generation:
--   generation 41 → PostgreSQL failure
--   → degraded generation 42
--   → recovered generation 43
-- All reconciliation, session, transport claim know which generation they belong to.
-- Prevents old incident operations from polluting new state.
--
-- Codex basis:
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A46/E22 — Audit: mandatory-ledger.

CREATE TABLE IF NOT EXISTS gptbridge_index.recovery_generation (
    generation_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    generation_number integer NOT NULL UNIQUE,
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    generation_type text NOT NULL CHECK (generation_type IN (
        'normal', 'degraded', 'recovered'
    )),
    previous_generation integer,
    started_at timestamptz NOT NULL DEFAULT now(),
    ended_at timestamptz,
    description text
);

ALTER TABLE gptbridge_index.recovery_generation ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.recovery_generation FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS recovery_gen_read ON gptbridge_index.recovery_generation;
CREATE POLICY recovery_gen_read ON gptbridge_index.recovery_generation
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS recovery_gen_write ON gptbridge_index.recovery_generation;
CREATE POLICY recovery_gen_write ON gptbridge_index.recovery_generation
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.recovery_generation FROM PUBLIC;
GRANT SELECT ON gptbridge_index.recovery_generation TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.recovery_generation
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS recovery_gen_number_idx
    ON gptbridge_index.recovery_generation (generation_number);

CREATE OR REPLACE FUNCTION gptbridge_index.create_recovery_generation(
    p_incident_id uuid,
    p_generation_type text,
    p_description text DEFAULT NULL
) RETURNS integer AS $$
DECLARE
    v_prev integer;
    v_new integer;
    v_id uuid;
BEGIN
    SELECT generation_number INTO v_prev
    FROM gptbridge_index.recovery_generation
    ORDER BY generation_number DESC LIMIT 1;

    v_new := COALESCE(v_prev, 0) + 1;

    UPDATE gptbridge_index.recovery_generation
    SET ended_at = now()
    WHERE generation_number = v_prev AND ended_at IS NULL;

    INSERT INTO gptbridge_index.recovery_generation (
        generation_number, incident_id, generation_type,
        previous_generation, description
    )
    VALUES (v_new, p_incident_id, p_generation_type, v_prev, p_description)
    RETURNING generation_id INTO v_id;

    RETURN v_new;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.get_current_generation()
RETURNS integer AS $$
DECLARE
    v_gen integer;
BEGIN
    SELECT generation_number INTO v_gen
    FROM gptbridge_index.recovery_generation
    ORDER BY generation_number DESC LIMIT 1;
    RETURN COALESCE(v_gen, 0);
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
