"""Core-system helpers shared by GPTBridge backend subsystems.

The System Sovereign is a Python + C++ hybrid architecture: the sovereign and
its in-process sub-sovereigns (RuntimeSubSovereign, ResourceSubSovereign,
DataSubSovereign, IntegrationSubSovereign and MaintenanceSovereign)
orchestrate in Python, delegating resource-/liveness-critical primitives to the
C++ native kernel (e.g. ``core_system.native``), which compiles to a .pyd.
"""

from .codex_decision import codex_edicts, decision_basis
from .data_sub_sovereign import DataSubSovereign
from .integration_sub_sovereign import IntegrationSubSovereign
from .maintenance_sovereign import MaintenanceSovereign
from .permission_sovereign import PermissionSovereign
from .resource_sub_sovereign import ResourceSubSovereign
from .runtime_sub_sovereign import RuntimeSubSovereign
from .system_sovereign import SystemSovereignService
from .xingcheng_coordination import XingchengCoordination

__all__ = [
    "DataSubSovereign",
    "IntegrationSubSovereign",
    "MaintenanceSovereign",
    "PermissionSovereign",
    "ResourceSubSovereign",
    "RuntimeSubSovereign",
    "SystemSovereignService",
    "XingchengCoordination",
    "codex_edicts",
    "decision_basis",
]


