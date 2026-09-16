-- 057_archive_versioning.sql
-- Archive Versioning.
--
-- Archives must not just output CSV and forget.  At minimum:
--   schema_version, release_id, encoding, created_at,
--   record_count, integrity_hash
--
-- This enables correct restore in the future.
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger.
--   A8/E21  — PostgreSQL: central-structured-official-data.

-- ============================================================================
-- archive_manifest — per-archive manifest embedded in catalog
-- Extends archive_catalog with versioning metadata.
-- ============================================================================

-- Add versioning columns to archive_catalog
ALTER TABLE gptbridge_index.archive_catalog
    ADD COLUMN IF NOT EXISTS encoding text NOT NULL DEFAULT 'utf-8';
ALTER TABLE gptbridge_index.archive_catalog
    ADD COLUMN IF NOT EXISTS archive_format_version integer NOT NULL DEFAULT 1;
ALTER TABLE gptbridge_index.archive_catalog
    ADD COLUMN IF NOT EXISTS checksum_algorithm text NOT NULL DEFAULT 'sha256';
ALTER TABLE gptbridge_index.archive_catalog
    ADD COLUMN IF NOT EXISTS restore_tested_at timestamptz;
ALTER TABLE gptbridge_index.archive_catalog
    ADD COLUMN IF NOT EXISTS restore_test_result jsonb;

-- ============================================================================
-- archive_version_manifest — view showing full versioning metadata
-- ============================================================================
CREATE OR REPLACE VIEW gptbridge_index.archive_version_manifest AS
SELECT
    archive_id,
    source_engine,
    source_schema,
    source_table,
    module_id,
    time_range_start,
    time_range_end,
    record_count,
    schema_version,
    release_id,
    storage_locator,
    storage_format,
    encoding,
    archive_format_version,
    integrity_hash,
    checksum_algorithm,
    created_at,
    verified_at,
    restore_tested_at,
    restore_test_result
FROM gptbridge_index.archive_catalog;

-- ============================================================================
-- mark_restore_tested() — mark an archive as restore-tested
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.mark_restore_tested(
    p_archive_id uuid,
    p_test_result jsonb
) RETURNS void AS $$
BEGIN
    UPDATE gptbridge_index.archive_catalog
    SET restore_tested_at = now(),
        restore_test_result = p_test_result
    WHERE archive_id = p_archive_id;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;

-- ============================================================================
-- get_untested_archives() — archives that haven't been restore-tested
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.get_untested_archives(
    p_limit integer DEFAULT 50
) RETURNS TABLE (
    archive_id uuid,
    source_table text,
    storage_locator text,
    created_at timestamptz
) AS $$
BEGIN
    RETURN QUERY
    SELECT archive_id, source_table, storage_locator, created_at
    FROM gptbridge_index.archive_catalog
    WHERE restore_tested_at IS NULL
    ORDER BY created_at
    LIMIT p_limit;
END;
$$ LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = pg_catalog, gptbridge_index;
