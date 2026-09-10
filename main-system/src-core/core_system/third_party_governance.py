"""Third-party sovereign, dependency boundary, and startup deadline — A197-E173.

This module implements three provisions from codex v2.81330:

  * **A197/E171** — third-party-sovereign-optimized-responsibility (max 3
    capabilities: identity-inventory-provenance, license-security-compatibility-
    risk, admission-version-lifecycle-decision).
  * **A198/E172** — local-code-and-third-party-dependency-boundary (local
    owned source vs immutable vendored/package-managed artifacts).
  * **A199/E173** — ten-second-complete-startup-deadline (10000ms monotonic
    hard deadline from entry-accepted to fully-ready).

All functions are **read-only verification and signal production**.  They
never download, install, mutate, activate, or bypass the information layer.

This file is a re-export facade; implementation lives in:
  * ``third_party_types``    — constants and dataclasses
  * ``third_party_verify``   — verification functions
  * ``third_party_signal``   — startup deadline check, verify, and signal
"""

from __future__ import annotations

from core_system.third_party_types import (
    ALLOWED_STACK,
    DEPENDENCY_BOUNDARY_FORBIDDEN,
    DependencyBoundaryCheck,
    DependencyIdentity,
    MAX_THIRD_PARTY_CAPABILITIES,
    STARTUP_DEADLINE_FORBIDDEN,
    STARTUP_HARD_DEADLINE_MS,
    STARTUP_PHASE_BUDGETS_MS,
    STARTUP_REQUIRED_FLOWS,
    THIRD_PARTY_DECIDES,
    THIRD_PARTY_DECISION_TYPES,
    THIRD_PARTY_DOES_NOT_DECIDE,
    THIRD_PARTY_FORBIDDEN_ACTIONS,
    THIRD_PARTY_SCOPE,
    THIRD_PARTY_SOVEREIGN_CAPABILITIES,
    ThirdPartyCapabilityCheck,
    ThirdPartyDecision,
)
from core_system.third_party_verify import (
    verify_dependency_boundary,
    verify_third_party_capabilities,
    verify_third_party_decision,
)
from core_system.third_party_signal import (
    StartupDeadlineCheck,
    startup_deadline_signal,
    verify_startup_deadline,
)

__all__ = [
    "ALLOWED_STACK",
    "DEPENDENCY_BOUNDARY_FORBIDDEN",
    "DependencyBoundaryCheck",
    "DependencyIdentity",
    "MAX_THIRD_PARTY_CAPABILITIES",
    "STARTUP_DEADLINE_FORBIDDEN",
    "STARTUP_HARD_DEADLINE_MS",
    "STARTUP_PHASE_BUDGETS_MS",
    "STARTUP_REQUIRED_FLOWS",
    "StartupDeadlineCheck",
    "THIRD_PARTY_DECIDES",
    "THIRD_PARTY_DECISION_TYPES",
    "THIRD_PARTY_DOES_NOT_DECIDE",
    "THIRD_PARTY_FORBIDDEN_ACTIONS",
    "THIRD_PARTY_SCOPE",
    "THIRD_PARTY_SOVEREIGN_CAPABILITIES",
    "ThirdPartyCapabilityCheck",
    "ThirdPartyDecision",
    "startup_deadline_signal",
    "verify_dependency_boundary",
    "verify_startup_deadline",
    "verify_third_party_capabilities",
    "verify_third_party_decision",
]
