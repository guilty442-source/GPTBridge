"""Core-system helpers shared by GPTBridge backend subsystems.

The Decision Sovereign is a Python + C++ hybrid architecture: the sovereign
and its in-process sub-sovereigns (RuntimeStateSyncSubSovereign,
ResourceDependencySyncSubSovereign, DataGovernanceSubSovereign,
ChannelContractSyncSubSovereign and HealthMaintenanceTestSubSovereign)
orchestrate in Python, delegating resource-/liveness-critical primitives to
the C++ native kernel (e.g. ``core_system.native``), which compiles to a
.pyd.

Per the amended Governance Codex (A302–A310/A322/A323/A327), the retired
sub-sovereign classes were superseded by the codex-aligned governance-layer
classes in ``main-system/governance/``.  The retired module paths under
``core_system`` remain as compatibility shims only; the active runtime
ownership path uses the classes exposed here.

Governance-layer names are resolved lazily (PEP 562) because the governance
package itself imports ``core_system.codex_decision`` — an eager import here
would deadlock on partially-initialized packages.
"""

from .codex_decision import codex_edicts, decision_basis
from .system_automation_coordinator import SystemAutomationCoordinator

_GOVERNANCE_EXPORTS = {
    "AutomaticLogSyncSubSovereign",
    "ChangeAcceptanceSubSovereign",
    "ChannelContractSyncSubSovereign",
    "CleanupRetentionSyncSubSovereign",
    "DataGovernanceSubSovereign",
    "DecisionSovereign",
    "DependencySyncSubSovereign",
    "DirectorySubSovereign",
    "HealthMaintenanceTestSubSovereign",
    "IdentityGroupSubSovereign",
    "LanguageReviewSubSovereign",
    "LearningEvidenceSyncSubSovereign",
    "PermissionSovereign",
    "PolicyArchitectureSubSovereign",
    "PriorityCapabilitySubSovereign",
    "ReleaseUpdateSyncSubSovereign",
    "RepairBackupSyncSubSovereign",
    "ResourceDependencySyncSubSovereign",
    "RuntimeStateSyncSubSovereign",
    "StartupSubSovereign",
    "SubSovereignBase",
    "SynchronizationSovereign",
    "SystemRuntimeSovereign",
    "SystemSubSovereign",
    "XingchengSovereign",
}

# Compatibility alias: the retired service name resolves to the merged
# governance-layer DecisionSovereign.
_SERVICE_ALIASES = {
    "DecisionSovereignService": "DecisionSovereign",
    "MaintenanceSovereign": "HealthMaintenanceTestSubSovereign",
    "DataSubSovereign": "DataGovernanceSubSovereign",
    "ResourceSubSovereign": "ResourceDependencySyncSubSovereign",
    "IntegrationSubSovereign": "ChannelContractSyncSubSovereign",
    "ThirdPartySubSovereign": "DependencySyncSubSovereign",
    "RuntimeSubSovereign": "RuntimeStateSyncSubSovereign",
    "SystemProgrammingSovereign": "ReleaseUpdateSyncSubSovereign",
    "LearningSystemSovereign": "LearningEvidenceSyncSubSovereign",
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
