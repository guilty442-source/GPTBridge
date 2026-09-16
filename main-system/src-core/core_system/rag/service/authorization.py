"""Permission Sovereign interface — authorization at TWO points.

    actor
      -> Scope Authorization      (may actor search module A at all?)
      -> Qdrant pre-filter        (gateway translates scope, never caller)
      -> retrieval
      -> Resource Authorization   (may actor read resource X?)
      -> Evidence Pool

``module_id`` is an isolation condition, not full authorization:
admission and per-resource checks are separate decisions.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class AdmissionDecision:
    allowed: bool
    module_scope: tuple[str, ...] = ()
    data_categories: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True, slots=True)
class ResourceGrant:
    resource_id: str
    allowed: bool
    reason: str = ""


class PermissionSovereign(Protocol):
    """Two-point authorization contract.  The concrete sovereign
    lives outside RAG; this is the only shape the service calls."""

    def admit_query(
        self,
        actor_id: str,
        module_ids: tuple[str, ...],
        operation: str,
    ) -> AdmissionDecision:
        """Point 1 — query admission: may the actor search this
        module scope for this operation?"""
        ...

    def authorize_resource(
        self,
        actor_id: str,
        resource_id: str,
        module_id: str,
        data_category: str,
    ) -> ResourceGrant:
        """Point 2 — per-resource authorization on each retrieval
        hit before it enters the evidence pool."""
        ...


@dataclass(slots=True)
class StaticPolicySovereign:
    """Reference implementation for tests/wiring — explicit grants.

    ``module_grants`` maps actor -> allowed module ids ("*" = any).
    ``resource_denies`` lists (actor, resource) pairs to deny.
    Anything unlisted is denied at resource level (fail closed) unless
    ``default_resource_allow`` is set.
    """

    module_grants: dict[str, frozenset[str]] = field(default_factory=dict)
    resource_denies: frozenset[tuple[str, str]] = frozenset()
    default_resource_allow: bool = False

    def admit_query(
        self,
        actor_id: str,
        module_ids: tuple[str, ...],
        operation: str,
    ) -> AdmissionDecision:
        granted = self.module_grants.get(actor_id, frozenset())
        if "*" in granted:
            return AdmissionDecision(True, module_ids, reason="wildcard")
        allowed = tuple(m for m in module_ids if m in granted)
        if not allowed:
            return AdmissionDecision(False, (), reason="no-module-grant")
        return AdmissionDecision(True, allowed, reason="scoped")

    def authorize_resource(
        self,
        actor_id: str,
        resource_id: str,
        module_id: str,
        data_category: str,
    ) -> ResourceGrant:
        if (actor_id, resource_id) in self.resource_denies:
            return ResourceGrant(resource_id, False, "explicit-deny")
        if not self.default_resource_allow:
            return ResourceGrant(resource_id, False, "fail-closed")
        return ResourceGrant(resource_id, True, "policy-allow")


__all__ = [
    "AdmissionDecision",
    "PermissionSovereign",
    "ResourceGrant",
    "StaticPolicySovereign",
]
