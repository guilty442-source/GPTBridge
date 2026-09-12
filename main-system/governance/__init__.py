"""GOVERNANCE_LAYER — 治理層套件 facade。

The ``sub_sovereigns`` directory name uses underscore (Python-compatible).
This facade loads it through ``importlib`` and re-exports the
full governance surface so callers can simply use::

    from governance import DataGovernanceSubSovereign
"""

from __future__ import annotations

from importlib import import_module as _import_module

from .sovereigns import (
    DecisionSovereign,
    PermissionSovereign,
    SovereignBase,
    SovereignIdentity,
    SynchronizationSovereign,
    SystemRuntimeSovereign,
    XingchengSovereign,
)

_sub_sovereigns = _import_module("governance.sub_sovereigns")

SubSovereignBase = _sub_sovereigns.SubSovereignBase
AutomaticLogSyncSubSovereign = _sub_sovereigns.AutomaticLogSyncSubSovereign
ChangeAcceptanceSubSovereign = _sub_sovereigns.ChangeAcceptanceSubSovereign
ChannelContractSyncSubSovereign = _sub_sovereigns.ChannelContractSyncSubSovereign
CleanupRetentionSyncSubSovereign = _sub_sovereigns.CleanupRetentionSyncSubSovereign
DataGovernanceSubSovereign = _sub_sovereigns.DataGovernanceSubSovereign
DependencySyncSubSovereign = _sub_sovereigns.DependencySyncSubSovereign
DirectorySubSovereign = _sub_sovereigns.DirectorySubSovereign
HealthMaintenanceTestSubSovereign = _sub_sovereigns.HealthMaintenanceTestSubSovereign
IdentityGroupSubSovereign = _sub_sovereigns.IdentityGroupSubSovereign
LanguageReviewSubSovereign = _sub_sovereigns.LanguageReviewSubSovereign
LearningEvidenceSyncSubSovereign = _sub_sovereigns.LearningEvidenceSyncSubSovereign
PolicyArchitectureSubSovereign = _sub_sovereigns.PolicyArchitectureSubSovereign
PriorityCapabilitySubSovereign = _sub_sovereigns.PriorityCapabilitySubSovereign
ReleaseUpdateSyncSubSovereign = _sub_sovereigns.ReleaseUpdateSyncSubSovereign
RepairBackupSyncSubSovereign = _sub_sovereigns.RepairBackupSyncSubSovereign
ResourceDependencySyncSubSovereign = _sub_sovereigns.ResourceDependencySyncSubSovereign
RuntimeStateSyncSubSovereign = _sub_sovereigns.RuntimeStateSyncSubSovereign
StartupSubSovereign = _sub_sovereigns.StartupSubSovereign
SystemSubSovereign = _sub_sovereigns.SystemSubSovereign

__all__ = [
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
    "SovereignBase",
    "SovereignIdentity",
    "StartupSubSovereign",
    "SubSovereignBase",
    "SynchronizationSovereign",
    "SystemRuntimeSovereign",
    "SystemSubSovereign",
    "XingchengSovereign",
]
