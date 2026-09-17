-- 129_permission_projection_runtime.sql
-- A501/A506: permission projection for the least-privilege runtime login.
--
-- The runtime DSN (``GPTBRIDGE_POSTGRES_DSN`` -> ``gptbridge_runtime``) serves
-- transport, central audit and RAG/index metadata writes on behalf of the
-- registered modules.  Enforcement reads ``gptbridge_security.principal`` and
-- ``gptbridge_security.principal_scope``; this migration records the approved
-- projection canonically so a rebuilt database replays it (A502).
--
-- The projection is idempotent: re-applying only raises flags, never removes
-- scopes.  Module scopes mirror the already registered module set.
--
-- Codex basis: A501 (runtime/admin separation), A506 (authorization
-- projection), A515/A516 (migration chain is the sole schema evolution
-- authority).

INSERT INTO gptbridge_security.principal
    (role_name, module_id, global_read, transport_execute, audit_write)
VALUES
    ('gptbridge_runtime', NULL, false, true, true)
ON CONFLICT (role_name) DO UPDATE
    SET transport_execute = true,
        audit_write = true;

INSERT INTO gptbridge_security.principal_scope
    (role_name, module_id, can_read, can_write)
SELECT 'gptbridge_runtime', module_id, true, true
FROM (
    SELECT DISTINCT module_id
    FROM gptbridge_security.principal_scope
    WHERE module_id IS NOT NULL
) AS registered_modules
ON CONFLICT (role_name, module_id) DO UPDATE
    SET can_read = true,
        can_write = true;
