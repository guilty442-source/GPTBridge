-- 056_archive_catalog.sql
-- Archive Catalog.
--
-- All archives must be findable.  Central record:
--   archive_id, source_engine, source_table, module_id, time_range,
--   record_count, schema_version, storage_locator, hash, created_at, verified_at
--
-- Without this, archives become "unknown files" over time.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- archive_catalog — central archive registry
-- ============================================================================
CREATE TABLE IF NOT EXISTS gptbridge_index.archive_catalog (
    archive_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_engine text NOT NULL CHECK (source_engine IN (
        'postgresql', 'sqlite', 'qdrant'
    )),
    source_schema text,
    source_table text NOT NULL,
    module_id text,
    time_range_start timestamptz NOT NULL,
    time_range_end timestamptz NOT NULL,
    record_count integer NOT NULL,
    schema_version integer NOT NULL,
    release_id text REFERENCES gptbridge_index.database_release(release_id),
    storage_locator text NOT NULL,  -- opaque locator, not raw path
    storage_format text NOT NULL DEFAULT 'csv' CHECK (storage_format IN (
        'csv', 'jsonl', 'parquet', 'compressed_csv', 'compressed_jsonl'
    )),
    integrity_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    verified_at timestamptz,
    verification_result jsonb,
    description text
);

ALTER TABLE gptbridge_index.archive_catalog ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.archive_catalog FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS archive_catalog_read ON gptbridge_index.archive_catalog;
CREATE POLICY archive_catalog_read ON gptbridge_index.archive_catalog
    FOR SELECT USING (gptbridge_security.can_read('governance_rule')
                      OR current_user LIKE 'gptbridge_%');

DROP POLICY IF EXISTS archive_catalog_write ON gptbridge_index.archive_catalog;
CREATE POLICY archive_catalog_write ON gptbridge_index.archive_catalog
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.archive_catalog FROM PUBLIC;
GRANT SELECT ON gptbridge_index.archive_catalog TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.archive_catalog
    TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS archive_catalog_source_idx
    ON gptbridge_index.archive_catalog (source_engine, source_table, created_at);
CREATE INDEX IF NOT EXISTS archive_catalog_time_idx
    ON gptbridge_index.archive_catalog (time_range_start, time_range_end);

-- ============================================================================
-- register_archive() — register a new archive
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.register_archive(
    p_source_engine text,
    p_source_table text,
    p_time_range_start timestamptz,
    p_time_range_end timestamptz,
    p_record_count integer,
    p_schema_version integer,
    p_storage_locator text,
    p_integrity_hash text,
    p_storage_format text DEFAULT 'csv',
    p_module_id text DEFAULT NULL,
    p_source_schema text DEFAULT NULL,
    p_release_id text DEFAULT NULL,
    p_description text DEFAULT NULL
) RETURNS uuid AS $$
DECLARE
    v_id uuid;
BEGIN
    INSERT INTO gptbridge_index.archive_catalog (
        source_engine, source_schema, source_table, module_id,
        time_range_start, time_range_end, record_count,
        schema_version, release_id, storage_locator, storage_format,
        integrity_hash, description
    )
    VALUES (
        p_source_engine, p_source_schema, p_source_table, p_module_id,
        p_time_range_start, p_time_range_end, p_record_count,
        p_schema_version, p_release_id, p_storage_locator, p_storage_format,
        p_integrity_hash, p_description
    )
    RETURNING archive_id INTO v_id;
    RETURN v_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- verify_archive() — mark an archive as verified
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.verify_archive(
    p_archive_id uuid,
    p_verification_result jsonb DEFAULT NULL
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.archive_catalog
    SET verified_at = now(),
        verification_result = p_verification_result
    WHERE archive_id = p_archive_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- find_archives() — find archives for a source table in a time range
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.find_archives(
    p_source_table text,
    p_time_start timestamptz DEFAULT NULL,
    p_time_end timestamptz DEFAULT NULL
) RETURNS TABLE (
    archive_id uuid,
    storage_locator text,
    record_count integer,
    created_at timestamptz,
    verified_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT archive_id, storage_locator, record_count, created_at, verified_at
    FROM gptbridge_index.archive_catalog
    WHERE source_table = p_source_table
      AND (p_time_start IS NULL OR time_range_end >= p_time_start)
      AND (p_time_end IS NULL OR time_range_start <= p_time_end)
    ORDER BY time_range_start;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
