"""RAG Administrative Commands — the control-plane surface.

Xingcheng's RAG Manager operates control plane only — never direct
point/table mutation:

    control plane (rag.reindex / rag.reconcile / rag.status level)
        rag.status               rag.health
        rag.generation.list      rag.generation.activate
        rag.reindex.resource     rag.reindex.module
        rag.reconciliation.run   rag.reconciliation.status
        rag.outbox.status        rag.benchmark.run

    high risk (requires rag.admin)
        rag.collection.rebuild   rag.generation.retire
        rag.resource.purge
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .capabilities import RagCapability


class AdminRisk(str, Enum):
    CONTROL_PLANE = "control-plane"
    HIGH_RISK = "high-risk"


@dataclass(frozen=True, slots=True)
class AdminCommandSpec:
    name: str
    risk: AdminRisk
    required_capability: RagCapability
    description: str = ""


_COMMANDS = (
    AdminCommandSpec("rag.status", AdminRisk.CONTROL_PLANE, RagCapability.STATUS),
    AdminCommandSpec("rag.health", AdminRisk.CONTROL_PLANE, RagCapability.STATUS),
    AdminCommandSpec("rag.generation.list", AdminRisk.CONTROL_PLANE, RagCapability.STATUS),
    AdminCommandSpec("rag.generation.activate", AdminRisk.CONTROL_PLANE, RagCapability.REINDEX),
    AdminCommandSpec("rag.reindex.resource", AdminRisk.CONTROL_PLANE, RagCapability.REINDEX),
    AdminCommandSpec("rag.reindex.module", AdminRisk.CONTROL_PLANE, RagCapability.REINDEX),
    AdminCommandSpec("rag.reconciliation.run", AdminRisk.CONTROL_PLANE, RagCapability.RECONCILE),
    AdminCommandSpec("rag.reconciliation.status", AdminRisk.CONTROL_PLANE, RagCapability.STATUS),
    AdminCommandSpec("rag.outbox.status", AdminRisk.CONTROL_PLANE, RagCapability.STATUS),
    AdminCommandSpec("rag.benchmark.run", AdminRisk.CONTROL_PLANE, RagCapability.REINDEX),
    AdminCommandSpec("rag.collection.rebuild", AdminRisk.HIGH_RISK, RagCapability.ADMIN),
    AdminCommandSpec("rag.generation.retire", AdminRisk.HIGH_RISK, RagCapability.ADMIN),
    AdminCommandSpec("rag.resource.purge", AdminRisk.HIGH_RISK, RagCapability.DELETE),
)

_COMMAND_INDEX = {c.name: c for c in _COMMANDS}


def get_admin_command(name: str) -> AdminCommandSpec | None:
    return _COMMAND_INDEX.get(name)


def list_admin_commands(risk: AdminRisk | None = None) -> tuple[AdminCommandSpec, ...]:
    if risk is None:
        return _COMMANDS
    return tuple(c for c in _COMMANDS if c.risk is risk)


def require_admin_capability(role: str, command_name: str) -> None:
    """Fail closed on unknown command; enforce capability tier."""
    spec = _COMMAND_INDEX.get(command_name)
    if spec is None:
        raise PermissionError(f"unknown admin command '{command_name}'")
    from .capabilities import require_capability
    require_capability(role, spec.required_capability)


__all__ = [
    "AdminCommandSpec",
    "AdminRisk",
    "get_admin_command",
    "list_admin_commands",
    "require_admin_capability",
]
