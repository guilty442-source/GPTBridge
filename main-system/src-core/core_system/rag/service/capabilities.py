"""RAG Capability model — what an actor may ask the service to do.

Capabilities gate every API entry point.  Xingcheng manages RAG day
to day (query/ingest/status plus bounded control-plane commands) but
never holds unrestricted admin:

    rag.query / rag.ingest / rag.status   — routine
    rag.reindex / rag.reconcile           — managed control plane
    rag.delete / rag.admin                — high-risk, elevated grant
"""
from __future__ import annotations

from enum import Enum


class RagCapability(str, Enum):
    QUERY = "rag.query"
    INGEST = "rag.ingest"
    DELETE = "rag.delete"
    REINDEX = "rag.reindex"
    STATUS = "rag.status"
    RECONCILE = "rag.reconcile"
    ADMIN = "rag.admin"


ROUTINE_CAPABILITIES = frozenset({
    RagCapability.QUERY,
    RagCapability.INGEST,
    RagCapability.STATUS,
})

CONTROL_PLANE_CAPABILITIES = frozenset({
    RagCapability.REINDEX,
    RagCapability.RECONCILE,
})

HIGH_RISK_CAPABILITIES = frozenset({
    RagCapability.DELETE,
    RagCapability.ADMIN,
})

# actor-role -> granted capabilities.  Xingcheng is a control-plane
# manager, not a database administrator.
ROLE_CAPABILITIES: dict[str, frozenset[RagCapability]] = {
    "xingcheng": ROUTINE_CAPABILITIES | CONTROL_PLANE_CAPABILITIES,
    "module": ROUTINE_CAPABILITIES,
    "agent": frozenset({RagCapability.QUERY, RagCapability.STATUS}),
    "ui": frozenset({RagCapability.QUERY, RagCapability.STATUS}),
    "admin": ROUTINE_CAPABILITIES | CONTROL_PLANE_CAPABILITIES | HIGH_RISK_CAPABILITIES,
}


def capabilities_for(role: str) -> frozenset[RagCapability]:
    """Unknown role -> no capabilities (fail closed)."""
    return ROLE_CAPABILITIES.get(role, frozenset())


def has_capability(role: str, capability: RagCapability) -> bool:
    return capability in capabilities_for(role)


def require_capability(role: str, capability: RagCapability) -> None:
    """Raise PermissionError when the role lacks the capability."""
    if not has_capability(role, capability):
        raise PermissionError(
            f"actor role '{role}' lacks capability '{capability.value}'"
        )


__all__ = [
    "CONTROL_PLANE_CAPABILITIES",
    "HIGH_RISK_CAPABILITIES",
    "ROLE_CAPABILITIES",
    "ROUTINE_CAPABILITIES",
    "RagCapability",
    "capabilities_for",
    "has_capability",
    "require_capability",
]
