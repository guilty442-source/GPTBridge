"""Core-system helpers shared by GPTBridge backend subsystems.

The Decision Sovereign is a Python + C++ hybrid architecture: the five
peer sovereign cores orchestrate in Python, delegating
resource-/liveness-critical primitives to the C++ native kernel (e.g.
``core_system.native``), which compiles to a .pyd.

A592/A604: the sub-sovereign layer is eliminated.  The retired
sub-sovereign classes and their compatibility aliases are removed; the
five peer cores coordinate through registered single-purpose modules
(FORBID:sub-sovereign-routing).  The A485 星澄-owned learning module
keeps its codex identity as lineage.

Governance-layer names are resolved lazily (PEP 562) because the governance
package itself imports ``core_system.codex_decision`` — an eager import here
would deadlock on partially-initialized packages.
"""

from .codex_decision import codex_edicts, decision_basis
from .system_automation_coordinator import SystemAutomationCoordinator

_GOVERNANCE_EXPORTS = {
    "AutomationSovereign",
    "DecisionSovereign",
    "PermissionSovereign",
    "SystemRuntimeSovereign",
    "XingchengSovereign",
}

# Compatibility aliases: retired service names resolve to the merged
# governance-layer cores / the single-entity 星澄 sovereign.
_SERVICE_ALIASES = {
    "DecisionSovereignService": "DecisionSovereign",
    "SynchronizationSovereign": "AutomationSovereign",
    "LearningSystemSovereign": "XingchengSovereign",
}


def __getattr__(name: str):
    target = _SERVICE_ALIASES.get(name, name)
    if target in _GOVERNANCE_EXPORTS:
        import governance

        return getattr(governance, target)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(
        set(globals())
        | _GOVERNANCE_EXPORTS
        | set(_SERVICE_ALIASES)
    )


__all__ = [
    "codex_edicts",
    "decision_basis",
    "SystemAutomationCoordinator",
    *_GOVERNANCE_EXPORTS,
    *_SERVICE_ALIASES,
]
