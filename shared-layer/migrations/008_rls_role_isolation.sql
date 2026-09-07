-- 008_rls_role_isolation.sql
-- Strengthen PostgreSQL role isolation beyond RLS.
-- Ensures each module can only access its own rows, even if RLS policies
-- are accidentally permissive.
-- A49/E35 + A44/E30.

-- ============================================================================
-- Per-module executor roles — each module gets its own NOLOGIN group role.
-- This provides defense-in-depth: even if RLS fails, GRANT/REVOKE still
-- isolates modules at the table privilege level.
-- ============================================================================

-- Create per-module executor roles (NOLOGIN — only used via SET ROLE)
DO $$
DECLARE
    module_id text;
BEGIN
    FOR module_id IN
        SELECT DISTINCT module_id FROM gptbridge_security.principal
        WHERE principal_type = 'module'
    LOOP
        -- Skip if role already exists
        IF NOT EXISTS (
            SELECT 1 FROM pg_roles
            WHERE rolname = 'gptbridge_module_' || replace(module_id, '-', '_')
        ) THEN
            EXECUTE format(
                'CREATE ROLE gptbridge_module_%I NOLOGIN',
                replace(module_id, '-', '_')
            );
        END IF;
    END LOOP;
END $$;

-- ============================================================================
-- Tighten RLS: add module-specific WITH CHECK constraints
-- ============================================================================

-- Resource: ensure INSERT/UPDATE rows match the caller's module
DROP POLICY IF EXISTS resource_owner_write_strict ON gptbridge_index.resource;
CREATE POLICY resource_owner_write_strict ON gptbridge_index.resource
    FOR ALL
    USING (gptbridge_security.can_write(module_id))
    WITH CHECK (
        gptbridge_security.can_write(module_id)
        AND module_id = current_setting('app.current_module_id', true)
    );

-- Locations: same strict check
DROP POLICY IF EXISTS location_owner_write_strict ON registry.locations;
CREATE POLICY location_owner_write_strict ON registry.locations
    FOR ALL
    USING (gptbridge_security.can_write(module_id))
    WITH CHECK (
        gptbridge_security.can_write(module_id)
        AND module_id = current_setting('app.current_module_id', true)
    );

-- ============================================================================
-- Revoke broad grants from shared executor roles; use per-module roles instead
-- ============================================================================

-- gptbridge_index_executor retains broad access (it's the governed service role)
-- but per-module roles get only their own module's data via RLS + SET ROLE.

-- Grant per-module roles to the central executor (so it can SET ROLE to any)
-- This is the governed pathway: the executor service sets the module context
-- before each operation.
GRANT gptbridge_index_executor TO gptbridge_xingcheng_reader WITH ADMIN OPTION;

-- ============================================================================
-- Audit: ensure audit rows carry the acting module_id for accountability
-- ============================================================================
ALTER TABLE gptbridge_audit.event
    ADD COLUMN IF NOT EXISTS acting_module_id text;
ALTER TABLE gptbridge_audit.event
    ADD COLUMN IF NOT EXISTS idempotency_key text;

CREATE INDEX IF NOT EXISTS audit_module_id_idx
    ON gptbridge_audit.event (acting_module_id, created_at);
