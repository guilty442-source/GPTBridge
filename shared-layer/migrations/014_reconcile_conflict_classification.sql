-- 014_reconcile_conflict_classification.sql
-- Reconcile conflict classification: instead of a single 'conflict' status,
-- classify conflicts into 6 types so downstream repair knows what to do.
--
-- Conflict types:
--   revision_conflict    — same version, different hash (true conflict)
--   missing_resource     — resource exists locally but not in central
--   hash_mismatch        — version matches but content hash differs
--   deleted_remote       — resource was deleted in central but still local
--   schema_mismatch      — local schema version != central schema version
--   authorization_changed — RLS/role policy changed, blocking the push
--
-- A44/E30 + A8/E21.

-- Add conflict_type column to the SQLite reconcile_state table (the SQLite
-- template is updated separately).  For PostgreSQL, we add a reconciliation
-- log table that records classified conflicts for audit and repair.

CREATE TABLE IF NOT EXISTS gptbridge_index.reconcile_conflict_log (
    conflict_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    module_id text NOT NULL,
    resource_id text NOT NULL,
    conflict_type text NOT NULL CHECK (
        conflict_type IN (
            'revision_conflict',
            'missing_resource',
            'hash_mismatch',
            'deleted_remote',
            'schema_mismatch',
            'authorization_changed'
        )
    ),
    local_version bigint,
    central_version bigint,
    local_hash text,
    central_hash text,
    detail text,
    detected_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    resolution_action text CHECK (
        resolution_action IN ('pending', 'local-wins', 'central-wins', 'manual', 'skipped')
    ) DEFAULT 'pending'
);

ALTER TABLE gptbridge_index.reconcile_conflict_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE gptbridge_index.reconcile_conflict_log FORCE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS reconcile_conflict_read ON gptbridge_index.reconcile_conflict_log;
CREATE POLICY reconcile_conflict_read ON gptbridge_index.reconcile_conflict_log
    FOR SELECT USING (gptbridge_security.can_read(module_id));

DROP POLICY IF EXISTS reconcile_conflict_write ON gptbridge_index.reconcile_conflict_log;
CREATE POLICY reconcile_conflict_write ON gptbridge_index.reconcile_conflict_log
    FOR ALL
    USING (gptbridge_security.can_write('governance_rule'))
    WITH CHECK (gptbridge_security.can_write('governance_rule'));

REVOKE ALL ON gptbridge_index.reconcile_conflict_log FROM PUBLIC;
GRANT SELECT ON gptbridge_index.reconcile_conflict_log TO gptbridge_index_reader;
GRANT SELECT, INSERT, UPDATE ON gptbridge_index.reconcile_conflict_log TO gptbridge_index_executor;

CREATE INDEX IF NOT EXISTS reconcile_conflict_module_idx
    ON gptbridge_index.reconcile_conflict_log (module_id, conflict_type, detected_at);
CREATE INDEX IF NOT EXISTS reconcile_conflict_unresolved_idx
    ON gptbridge_index.reconcile_conflict_log (resolution_action)
    WHERE resolution_action = 'pending';
