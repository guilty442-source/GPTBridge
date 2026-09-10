"""Deadlock-free governed startup — A192/E167 and A191/E166.

Per A192 (deadlock-free-governed-startup), A191 (startup-dependency-
classification-and-legacy-order-retirement), E167 (startup-liveness), and
E166 (startup-dependency-criticality), the startup sequence is a
deadlock-free, phase-ordered, dependency-DAG-driven process owned by the
startup sovereign.

This module is a re-export facade; the implementation lives in submodules:

  * :mod:`core_system.governed_startup_types` — constants and dataclasses.
  * :mod:`core_system.governed_startup_verify` — verification functions and DAG.
  * :mod:`core_system.governed_startup_signal` — signal functions.
"""

from __future__ import annotations

from core_system.governed_startup_signal import (
    startup_failure_signal,
    startup_status,
)
from core_system.governed_startup_types import (
    BOOTSTRAP_CAPABILITY_PROPERTIES,
    CORE_READY_CONDITIONS,
    DEPENDENCY_CRITICALITY_CLASSES,
    DependencyDeclaration,
    NO_FIXED_CRITICALITY_SERVICES,
    STARTUP_PHASES,
    StartupGeneration,
)
from core_system.governed_startup_verify import (
    DependencyDAG,
    verify_core_ready,
    verify_dependency_classification,
    verify_phase_order,
)

__all__ = [
    "BOOTSTRAP_CAPABILITY_PROPERTIES",
    "CORE_READY_CONDITIONS",
    "DEPENDENCY_CRITICALITY_CLASSES",
    "DependencyDAG",
    "DependencyDeclaration",
    "NO_FIXED_CRITICALITY_SERVICES",
    "STARTUP_PHASES",
    "StartupGeneration",
    "startup_failure_signal",
    "startup_status",
    "verify_core_ready",
    "verify_dependency_classification",
    "verify_phase_order",
]
