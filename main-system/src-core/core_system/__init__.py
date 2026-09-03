"""Core-system helpers shared by GPTBridge backend subsystems.

The System Sovereign is a Python + C++ hybrid architecture: the sovereign and
its two in-process sub-sovereigns (RuntimeSovereign and MaintenanceSovereign)
orchestrate in Python, delegating resource-/liveness-critical primitives to the
C++ native kernel (e.g. ``core_system.native``), which compiles to a .pyd.
"""

from .codex_decision import codex_edicts, decision_basis
from .data_sovereign import DataSovereign
from .integration_sovereign import IntegrationSovereign
from .maintenance_sovereign import MaintenanceSovereign
from .permission_sovereign import PermissionSovereign
from .resource_sovereign import ResourceSovereign
from .runtime_sovereign import RuntimeSovereign
from .system_sovereign import SystemSovereignService
from .xingcheng_coordination import XingchengCoordination

__all__ = [
    "DataSovereign",
    "IntegrationSovereign",
    "MaintenanceSovereign",
    "PermissionSovereign",
    "ResourceSovereign",
    "RuntimeSovereign",
    "SystemSovereignService",
    "XingchengCoordination",
    "codex_edicts",
    "decision_basis",
]


