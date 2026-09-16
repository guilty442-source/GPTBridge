-- 100_qdrant_full_rebuild.sql
-- Qdrant Full Rebuild.
--
-- Severe damage: create new collection generation → read PG chunk metadata →
-- re-embed → populate new collection → consistency verification →
-- switch active collection → retire old collection.
-- Qdrant can be entirely deleted and rebuilt from PG metadata + resource lineage.
--
-- Codex basis:
--   A52/E38 — RAG: Qdrant canonical semantic index.
--   A8/E21  — PostgreSQL: central-structured-official-data.

CREATE TABLE IF NOT EXISTS gptbridge_index.qdrant_full_rebuild (
    rebuild_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id uuid REFERENCES gptbridge_index.recovery_incident(incident_id),
    old_collection_name text,
    new_collection_name text NOT NULL,
    collection_generation integer NOT NULL,
    status text NOT NULL DEFAULT 'creating' CHECK (status IN (
        'creating', 'reading_metadata', 'reembedding',
        'populating', 'verifying', 'switching', 'completed', 'failed'
    )),
    total_chunks integer NOT NULL DEFAULT 0,
    processed_chunks integer NOT NULL DEFAULT 0,
    verified_chunks integer NOT NULL DEFAULT 0,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    switched_at timestamptz,
    old_collection_retired boolean NOT NULL DEFAULT false,
    failure_reason text
);

ALTER TABLE gptbridge_index.qdrant_full_rebuild ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.qdrant_full_rebuild FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS qdrant_full_rebuild_read ON gptbridge_index.qdrant_full_rebuild;
CREATE POLICY qdrant_full_rebuild_read ON gptbridge_index.qdrant_full_rebuild
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS qdrant_full_rebuild_write ON gptbridge_index.qdrant_full_rebuild;
CREATE POLICY qdrant_full_rebuild_write ON gptbridge_index.qdrant_full_rebuild
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.qdrant_full_rebuild FROM PUBLIC;
GRANT SELECT ON gptbridge_index.qdrant_full_rebuild TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.qdrant_full_rebuild
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS qdrant_rebuild_status_idx
    ON gptbridge_index.qdrant_full_rebuild (status, started_at);

CREATE OR REPLACE FUNCTION gptbridge_index.start_qdrant_full_rebuild(
    p_incident_id uuid,
    p_new_collection_name text,
    p_collection_generation integer,
    p_old_collection_name text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.qdrant_full_rebuild (
        incident_id, old_collection_name, new_collection_name,
        collection_generation
    )
    VALUES (
        p_incident_id, p_old_collection_name, p_new_collection_name,
        p_collection_generation
    )
    RETURNING rebuild_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

CREATE OR REPLACE FUNCTION gptbridge_index.advance_qdrant_rebuild(
    p_rebuild_id uuid,
    p_new_status text,
    p_total integer DEFAULT 0,
    p_processed integer DEFAULT 0,
    p_verified integer DEFAULT 0,
    p_failure_reason text DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.qdrant_full_rebuild
    SET status = p_new_status,
        total_chunks = CASE WHEN p_total > 0 THEN p_total ELSE total_chunks END,
        processed_chunks = processed_chunks + p_processed,
        verified_chunks = verified_chunks + p_verified,
        switched_at = CASE WHEN p_new_status = 'switching' THEN now() ELSE switched_at END,
        completed_at = CASE WHEN p_new_status IN ('completed', 'failed') THEN now() ELSE completed_at END,
        old_collection_retired = CASE WHEN p_new_status = 'completed' THEN true ELSE old_collection_retired END,
        failure_reason = p_failure_reason
    WHERE rebuild_id = p_rebuild_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
