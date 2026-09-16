-- 020_write_provenance.sql
-- Write Provenance: every INSERT/UPDATE carries actor_id, executor_id,
-- decision_id, correlation_id, and source_revision — not just "data changed".
--
-- Codex basis:
--   A46/E22 — Audit: mandatory-ledger; write=governed-executor.
--   A8/E21  — PostgreSQL: central-structured-official-data.
--   A10/E10 — explicit-allowlist; deny-by-default.
--
-- This migration adds provenance columns to:
--   * gptbridge_index.resource
--   * gptbridge_rag.index_state
--   * gptbridge_audit.event
-- and adds triggers that auto-populate them from session variables
-- (gptbridge.actor_id, gptbridge.executor_id, gptbridge.decision_id,
--  gptbridge.correlation_id, gptbridge.source_revision) set by the
-- runtime provenance helper (A5).
--
-- The data_lineage table (migration 018) already captures the same
-- fields for cross-engine tracing; these columns keep the provenance
-- visible on the row itself for fast local queries without a join.

-- ============================================================================
-- gptbridge_index.resource — provenance columns
-- ============================================================================
ALTER TABLE gptbridge_index.resource
    ADD COLUMN IF NOT EXISTS executor_id text,
    ADD COLUMN IF NOT EXISTS correlation_id text,
    ADD COLUMN IF NOT EXISTS source_revision bigint;

CREATE INDEX IF NOT EXISTS resource_correlation_idx
    ON gptbridge_index.resource (correlation_id)
    WHERE correlation_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS resource_executor_idx
    ON gptbridge_index.resource (executor_id)
    WHERE executor_id IS NOT NULL;

-- ============================================================================
-- gptbridge_rag.index_state — provenance columns
-- ============================================================================
ALTER TABLE gptbridge_rag.index_state
    ADD COLUMN IF NOT EXISTS executor_id text,
    ADD COLUMN IF NOT EXISTS correlation_id text;

-- source_revision already exists on index_state (migration 009).

CREATE INDEX IF NOT EXISTS index_state_correlation_idx
    ON gptbridge_rag.index_state (correlation_id)
    WHERE correlation_id IS NOT NULL;

-- ============================================================================
-- gptbridge_audit.event — provenance columns
-- ============================================================================
ALTER TABLE gptbridge_audit.event
    ADD COLUMN IF NOT EXISTS executor_id text,
    ADD COLUMN IF NOT EXISTS correlation_id text,
    ADD COLUMN IF NOT EXISTS source_revision bigint;

CREATE INDEX IF NOT EXISTS audit_event_correlation_idx
    ON gptbridge_audit.event (correlation_id)
    WHERE correlation_id IS NOT NULL;

-- ============================================================================
-- Auto-populate provenance on resource INSERT/UPDATE.
-- Reads session variables; falls back to NULL when not set (safe for
-- migrations and manual repairs).
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_index.auto_populate_provenance()
RETURNS trigger AS $$
DECLARE
    v_executor text;
    v_correlation text;
    v_source_rev bigint;
BEGIN
    v_executor := nullif(current_setting('gptbridge.executor_id', true), '');
    v_correlation := nullif(current_setting('gptbridge.correlation_id', true), '');
    v_source_rev := nullif(current_setting('gptbridge.source_revision', true), '')::bigint;

    IF v_executor IS NOT NULL THEN
        NEW.executor_id := v_executor;
    END IF;
    IF v_correlation IS NOT NULL THEN
        NEW.correlation_id := v_correlation;
    END IF;
    IF v_source_rev IS NOT NULL THEN
        NEW.source_revision := v_source_rev;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS resource_provenance_populate ON gptbridge_index.resource;
CREATE TRIGGER resource_provenance_populate
    BEFORE INSERT OR UPDATE ON gptbridge_index.resource
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_index.auto_populate_provenance();

-- ============================================================================
-- Auto-populate provenance on index_state INSERT/UPDATE.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_rag.auto_populate_provenance()
RETURNS trigger AS $$
DECLARE
    v_executor text;
    v_correlation text;
BEGIN
    v_executor := nullif(current_setting('gptbridge.executor_id', true), '');
    v_correlation := nullif(current_setting('gptbridge.correlation_id', true), '');

    IF v_executor IS NOT NULL THEN
        NEW.executor_id := v_executor;
    END IF;
    IF v_correlation IS NOT NULL THEN
        NEW.correlation_id := v_correlation;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS index_state_provenance_populate ON gptbridge_rag.index_state;
CREATE TRIGGER index_state_provenance_populate
    BEFORE INSERT OR UPDATE ON gptbridge_rag.index_state
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_rag.auto_populate_provenance();

-- ============================================================================
-- Auto-populate provenance on audit.event INSERT.
-- ============================================================================
CREATE OR REPLACE FUNCTION gptbridge_audit.auto_populate_provenance()
RETURNS trigger AS $$
DECLARE
    v_executor text;
    v_correlation text;
    v_source_rev bigint;
BEGIN
    v_executor := nullif(current_setting('gptbridge.executor_id', true), '');
    v_correlation := nullif(current_setting('gptbridge.correlation_id', true), '');
    v_source_rev := nullif(current_setting('gptbridge.source_revision', true), '')::bigint;

    IF v_executor IS NOT NULL THEN
        NEW.executor_id := v_executor;
    END IF;
    IF v_correlation IS NOT NULL THEN
        NEW.correlation_id := v_correlation;
    END IF;
    IF v_source_rev IS NOT NULL THEN
        NEW.source_revision := v_source_rev;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS audit_event_provenance_populate ON gptbridge_audit.event;
CREATE TRIGGER audit_event_provenance_populate
    BEFORE INSERT ON gptbridge_audit.event
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_audit.auto_populate_provenance();
