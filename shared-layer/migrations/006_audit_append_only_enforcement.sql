-- 006_audit_append_only_enforcement.sql
-- gptbridge_audit.event is append-only.
-- No role (except governance-authority) may UPDATE or DELETE audit rows.
-- A46/E22 — audit ledger integrity.

-- Revoke UPDATE and DELETE from all executor roles
REVOKE UPDATE, DELETE ON gptbridge_audit.event FROM gptbridge_index_executor;
REVOKE UPDATE, DELETE ON gptbridge_audit.event FROM gptbridge_transport_executor;

-- Only allow INSERT for executor roles (append-only)
-- (SELECT + INSERT grants are already in central_index.sql; this is explicit)
GRANT SELECT, INSERT ON gptbridge_audit.event TO gptbridge_index_executor;
GRANT SELECT, INSERT ON gptbridge_audit.event TO gptbridge_transport_executor;

-- RLS: no UPDATE or DELETE policy → all UPDATE/DELETE blocked for non-superuser
-- Even if a role somehow gets UPDATE privilege, RLS has no UPDATE policy,
-- so the operation is denied (FORCE RLS is already set in central_index.sql).
DROP POLICY IF EXISTS event_update ON gptbridge_audit.event;
DROP POLICY IF EXISTS event_delete ON gptbridge_audit.event;

-- Add a trigger to prevent UPDATE/DELETE even if someone bypasses RLS
-- (defense in depth: trigger fires before RLS check for superusers)
CREATE OR REPLACE FUNCTION gptbridge_audit.prevent_audit_mutation()
RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'AUDIT_APPEND_ONLY: UPDATE and DELETE are forbidden on gptbridge_audit.event (A46/E22)';
END;
$$ LANGUAGE plpgsql IMMUTABLE;

DROP TRIGGER IF EXISTS audit_no_update ON gptbridge_audit.event;
CREATE TRIGGER audit_no_update
    BEFORE UPDATE ON gptbridge_audit.event
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_audit.prevent_audit_mutation();

DROP TRIGGER IF EXISTS audit_no_delete ON gptbridge_audit.event;
CREATE TRIGGER audit_no_delete
    BEFORE DELETE ON gptbridge_audit.event
    FOR EACH ROW
    EXECUTE FUNCTION gptbridge_audit.prevent_audit_mutation();
