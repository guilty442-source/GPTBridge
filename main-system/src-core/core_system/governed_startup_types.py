"""Deadlock-free governed startup — types and constants (A192/E167, A191/E166).

Constants and data structures for the startup sequence.  Split from
``governed_startup`` for A185/E160 source-size compliance.

A191/A192: phase lists, criticality classes, and ready-conditions are
loaded from ``config/startup_manifest.json`` via
``startup_core.startup_config`` so the startup DAG can be adjusted
without source-code changes.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final

from startup_core.startup_config import governed_startup_constant as _cfg_gs

# ---------------------------------------------------------------------------
# Startup phases (A192: PHASE-0 through PHASE-6) — loaded from config
# ---------------------------------------------------------------------------

STARTUP_PHASES: Final[tuple[str, ...]] = _cfg_gs("startup_phases")

# A192: CORE-READY — all conditions that must hold for core-ready
CORE_READY_CONDITIONS: Final[tuple[str, ...]] = _cfg_gs("core_ready_conditions")

# A192: BOOTSTRAP-CAPABILITY properties
BOOTSTRAP_CAPABILITY_PROPERTIES: Final[tuple[str, ...]] = _cfg_gs("bootstrap_capability_properties")

# ---------------------------------------------------------------------------
# Dependency criticality classes (A191) — loaded from config
# ---------------------------------------------------------------------------

DEPENDENCY_CRITICALITY_CLASSES: Final[tuple[str, ...]] = _cfg_gs("dependency_criticality_classes")

# A191: services with no global fixed criticality
NO_FIXED_CRITICALITY_SERVICES: Final[tuple[str, ...]] = _cfg_gs("no_fixed_criticality_services")


# ---------------------------------------------------------------------------
# Dependency declaration (A191: DEPENDENCY-DECLARATION)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DependencyDeclaration:
    """A certified-manifest dependency declaration (A191).

    Per A191: ``DEPENDENCY-DECLARATION:each-service states identity+owner+
    required-by+criticality+readiness-contract+deadline+retry-budget+
    shutdown-order``.
    """

    identity: str
    owner: str
    required_by: str
    criticality: str  # "core-critical" | "capability-critical" | "optional"
    readiness_contract: str
    deadline: str
    retry_budget: int
    shutdown_order: int

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_core_critical(self) -> bool:
        return self.criticality == "core-critical"

    @property
    def is_capability_critical(self) -> bool:
        return self.criticality == "capability-critical"

    @property
    def is_optional(self) -> bool:
        return self.criticality == "optional"

    @property
    def valid_criticality(self) -> bool:
        return self.criticality in DEPENDENCY_CRITICALITY_CLASSES


# ---------------------------------------------------------------------------
# Startup generation (A192: SINGLE-FLIGHT)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class StartupGeneration:
    """A single-flight startup generation (A192: SINGLE-FLIGHT).

    Per A192: ``SINGLE-FLIGHT:one-startup-generation``.
    """

    generation_id: str
    release_id: str
    started_at: str
    current_phase: str
    core_ready: bool
    deferred_active: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
