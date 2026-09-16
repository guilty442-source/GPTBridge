"""RAG Service boundary — the governed information service layer.

    Modules / Xingcheng / UI / Agent
        -> Information Channel (RagQueryCommand — intent only)
        -> Permission Sovereign (admission + per-resource)
        -> RagApplicationService (5 entry points)
        -> Canonical Gateway -> PostgreSQL / Qdrant
        -> Retrievers -> Evidence

Qdrant is reachable only through this service's canonical gateway.
"""
from .admin_commands import (
    AdminCommandSpec,
    AdminRisk,
    get_admin_command,
    list_admin_commands,
    require_admin_capability,
)
from .audit import RagAuditRecord, make_audit_record
from .authorization import (
    AdmissionDecision,
    PermissionSovereign,
    ResourceGrant,
    StaticPolicySovereign,
)
from .capabilities import (
    HIGH_RISK_CAPABILITIES,
    ROUTINE_CAPABILITIES,
    RagCapability,
    capabilities_for,
    has_capability,
    require_capability,
)
from .commands import (
    CommandRejected,
    RagDeleteCommand,
    RagIngestCommand,
    RagQueryCommand,
    RagReconcileCommand,
    RagStatusCommand,
)
from .content_resolver import (
    ContentResolver,
    ContentResolverRegistry,
    DenyAllResolver,
    ResolvedContent,
)
from .resource_registry import RagResource, RagResourceRegistry
from .response import RagResponse, RetrievalMetadata
from .service_api import RagApplicationService, ServiceResult

__all__ = [
    "AdminCommandSpec",
    "AdminRisk",
    "AdmissionDecision",
    "CommandRejected",
    "ContentResolver",
    "ContentResolverRegistry",
    "DenyAllResolver",
    "HIGH_RISK_CAPABILITIES",
    "PermissionSovereign",
    "ROUTINE_CAPABILITIES",
    "RagApplicationService",
    "RagAuditRecord",
    "RagCapability",
    "RagDeleteCommand",
    "RagIngestCommand",
    "RagQueryCommand",
    "RagReconcileCommand",
    "RagResource",
    "RagResourceRegistry",
    "RagResponse",
    "RagStatusCommand",
    "ResolvedContent",
    "ResourceGrant",
    "RetrievalMetadata",
    "ServiceResult",
    "StaticPolicySovereign",
    "capabilities_for",
    "get_admin_command",
    "has_capability",
    "list_admin_commands",
    "make_audit_record",
    "require_admin_capability",
    "require_capability",
]
