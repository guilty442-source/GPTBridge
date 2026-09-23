"""Sovereign Stack Executor — constants and helpers.

Provides the child identity to class name mapping and helper
functions used by the SovereignStackExecutor.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any


def _sub_sovereigns_module() -> Any:
    return import_module("governance.sub_sovereigns")


# child_identity -> class reference.
#   * plain name            -> class in ``governance.sub_sovereigns``
#   * ``module:ClassName``  -> dotted module reference (A485 owner moves)
_CHILD_CLASSES: dict[str, str] = {
    "system-sub-sovereign": "SystemSubSovereign",
    "startup-sub-sovereign": "StartupSubSovereign",
    "directory-sub-sovereign": "DirectorySubSovereign",
    "identity-group-sub-sovereign": "IdentityGroupSubSovereign",
    "policy-architecture-sub-sovereign": "PolicyArchitectureSubSovereign",
    "health-maintenance-test-sub-sovereign": "HealthMaintenanceTestSubSovereign",
    "data-governance-sub-sovereign": "DataGovernanceSubSovereign",
    "priority-capability-sub-sovereign": "PriorityCapabilitySubSovereign",
    "change-acceptance-sub-sovereign": "ChangeAcceptanceSubSovereign",
    "resource-dependency-sync-sub-sovereign": "ResourceDependencySyncSubSovereign",
    "dependency-sync-sub-sovereign": "DependencySyncSubSovereign",
    "channel-contract-sync-sub-sovereign": "ChannelContractSyncSubSovereign",
    "release-update-sync-sub-sovereign": "ReleaseUpdateSyncSubSovereign",
    # A485: the learning sub-sovereign is a privileged-institution-managed
    # child of 星澄 and its implementation lives in the 星澄 owner package.
    "learning-evidence-sync-sub-sovereign": (
        "governance.sovereigns.xingcheng.learning_sub_sovereign:"
        "LearningEvidenceSyncSubSovereign"
    ),
    "runtime-state-sync-sub-sovereign": "RuntimeStateSyncSubSovereign",
    "repair-backup-sync-sub-sovereign": "RepairBackupSyncSubSovereign",
    "cleanup-retention-sync-sub-sovereign": "CleanupRetentionSyncSubSovereign",
    "automatic-log-sync-sub-sovereign": "AutomaticLogSyncSubSovereign",
}


def _resolve_child_class(class_ref: str) -> Any:
    """Resolve a child class reference (plain name or ``module:Class``)."""
    module_name, separator, class_name = str(class_ref).partition(":")
    if not separator:
        return getattr(_sub_sovereigns_module(), module_name)
    return getattr(import_module(module_name), class_name)


# Per-child start kwargs resolved at dispatch time.
def _child_start_kwargs(app: Any, child_id: str) -> dict[str, Any]:
    if child_id == "resource-dependency-sync-sub-sovereign":
        return {"memory_maintainer": getattr(app, "_idle_memory_maintainer", None)}
    return {}


__all__ = [
    "_sub_sovereigns_module",
    "_CHILD_CLASSES",
    "_child_start_kwargs",
    "_resolve_child_class",
]
