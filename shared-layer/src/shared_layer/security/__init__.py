"""Access control plane for the local data platform.

Covered here:

  * DSN purpose separation (runtime / reader / admin / backup),
  * short-term session identity binding,
  * role layering + least-privilege certification,
  * credential rotation with grace period and emergency revocation,
  * security generation fence for sensitive writes,
  * secret metadata registry (no plaintext in PostgreSQL),
  * SQLite file/path/scope boundaries,
  * mandatory Qdrant query scoping,
  * credential audit events.
"""

from .audit import (
    AUDIT_INSERT_QUERY_KEY,
    CredentialAuditEvent,
    CredentialEvent,
    assert_no_secret_material,
)
from .dsn_policy import (
    DsnBinding,
    DsnPolicyError,
    DsnPurpose,
    assert_no_admin_privileges,
    assert_separated_credentials,
    resolve_dsn,
    runtime_context_active,
)
from .generation import (
    GenerationLedger,
    SecurityGenerationError,
    SENSITIVE_WORKLOADS,
    assert_generation_current,
)
from .qdrant_scope import (
    QdrantScopeError,
    ScopedFilter,
    assert_payload_scoped,
    require_scope,
)
from .roles import (
    FORBIDDEN_ROLE_FLAGS,
    GOVERNED_SCHEMAS,
    LeastPrivilegeReport,
    certification_errors,
    least_privilege_report,
)
from .rotation import (
    EMERGENCY_REVOKE_SEQUENCE,
    CredentialVersion,
    EmergencyRevocation,
    RotationError,
    RotationPhase,
    RotationPlan,
)
from .secrets import (
    SecretMetadata,
    SecretPolicyError,
    SecretRegistry,
    assert_metadata_only,
    fingerprint,
    verify_fingerprint,
)
from .session_identity import (
    SessionIdentity,
    apply_session_identity,
    identity_sql_statements,
)
from .sqlite_scope import (
    SqliteAccessRequest,
    SqliteScopeBinding,
    SqliteScopeError,
    assess_access,
    assert_binding,
    expected_acl_commands,
    writable_roots,
)

__all__ = [
    "AUDIT_INSERT_QUERY_KEY",
    "CredentialAuditEvent",
    "CredentialEvent",
    "CredentialVersion",
    "DsnBinding",
    "DsnPolicyError",
    "DsnPurpose",
    "EMERGENCY_REVOKE_SEQUENCE",
    "EmergencyRevocation",
    "FORBIDDEN_ROLE_FLAGS",
    "GOVERNED_SCHEMAS",
    "GenerationLedger",
    "LeastPrivilegeReport",
    "QdrantScopeError",
    "RotationError",
    "RotationPhase",
    "RotationPlan",
    "SENSITIVE_WORKLOADS",
    "ScopedFilter",
    "SecretMetadata",
    "SecretPolicyError",
    "SecretRegistry",
    "SecurityGenerationError",
    "SessionIdentity",
    "SqliteAccessRequest",
    "SqliteScopeBinding",
    "SqliteScopeError",
    "apply_session_identity",
    "assert_generation_current",
    "assert_metadata_only",
    "assert_no_admin_privileges",
    "assert_no_secret_material",
    "assert_payload_scoped",
    "assert_separated_credentials",
    "assess_access",
    "assert_binding",
    "certification_errors",
    "expected_acl_commands",
    "fingerprint",
    "identity_sql_statements",
    "least_privilege_report",
    "require_scope",
    "resolve_dsn",
    "runtime_context_active",
    "verify_fingerprint",
    "writable_roots",
]
