"""Core-system helpers shared by GPTBridge backend subsystems.

The Decision Sovereign is a Python + C++ hybrid architecture: the five
peer sovereign cores orchestrate in Python, delegating
resource-/liveness-critical primitives to the C++ native kernel (e.g.
``core_system.native``), which compiles to a .pyd.

A592/A604: the sub-sovereign layer is eliminated.  The retired
sub-sovereign classes and their compatibility aliases are removed; the
five peer cores govern registered single-purpose modules
(FORBID:sub-sovereign-routing).  The A485 learning capability is
intrinsic to the single-entity 星澄 sovereign; the retired codex
identity survives only as lineage.

Governance-layer names are resolved lazily (PEP 562) because the governance
package itself imports ``core_system.codex_decision`` — an eager import here
would deadlock on partially-initialized packages.

``codex_edicts`` / ``decision_basis`` / ``SystemAutomationCoordinator``
resolve lazily for the same reason: ``codex_decision`` reaches the
PostgreSQL codex adapter (psycopg), and coupling package import to that
optional driver makes every ``core_system`` consumer unimportable.
"""

_MODULE_EXPORTS = {
    "codex_edicts": ".codex_decision",
    "decision_basis": ".codex_decision",
    "SystemAutomationCoordinator": ".system_automation_coordinator",
}

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
    module_rel = _MODULE_EXPORTS.get(name)
    if module_rel is not None:
        import importlib

        module = importlib.import_module(module_rel, __name__)
        return getattr(module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(
        set(globals())
        | _GOVERNANCE_EXPORTS
        | set(_MODULE_EXPORTS)
        | set(_SERVICE_ALIASES)
    )


__all__ = [
    "codex_edicts",
    "decision_basis",
    "SystemAutomationCoordinator",
    *_GOVERNANCE_EXPORTS,
    *_SERVICE_ALIASES,
]
