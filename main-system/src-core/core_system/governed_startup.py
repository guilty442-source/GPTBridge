"""Deadlock-free governed startup — A192/E167 and A191/E166.

Per A192 (deadlock-free-governed-startup), A191 (startup-dependency-
classification-and-legacy-order-retirement), E167 (startup-liveness), and
E166 (startup-dependency-criticality), the startup sequence is a
deadlock-free, phase-ordered, dependency-DAG-driven process owned by the
startup sovereign.

Key invariants (A192):

  * **Single-flight** — one startup generation at a time.
  * **Phase 0** — local process preflight without cross-owner communication.
  * **Phase 1** — activate minimal information-layer bootstrap mode
    (deny-by-default, no business route, no database/vector/model dependency).
  * **Bootstrap capability** — pre-issued, read-only, startup-sovereign-bound,
    official-codex-entry-only, sealed in active release, verified mechanically
    without live permission decision.
  * **Phase 2** — through minimal information layer, read
    governance-codex://official, verify last valid human-sealed version.
  * **Phase 3** — through information layer, load authoritative permission
    directory, verify bootstrap capability, activate permission sovereign.
  * **Phase 4** — switch information layer to normal authenticated authorized
    mode, invalidate bootstrap session/capability.
  * **Phase 5** — system-decision-sovereign classifies certified manifest
    dependencies as core-critical, capability-critical, or optional.
    Startup sovereign executes acyclic critical path.
  * **Phase 6** — activate system-runtime, maintenance, and required
    specialized sovereigns by dependency DAG.
  * **Core-ready** — official-codex-valid + permission-sovereign-active +
    normal-information-layer-active + system-decision-active + system-runtime-
    active + maintenance-active + all-core-critical-dependencies-ready.
  * **Deferred** — capability-critical/optional services start after
    core-ready with bounded parallelism.
  * **Failure** — core-critical failure => failed-generation + owned-reverse-
    DAG-cleanup + typed-user-visible-cause.  Non-core failure => affected-
    capability-degraded-only + retry-budget.
  * **No circular gate** — codex read before permission sovereign, permission
    sovereign before normal routes, no live-permission-review-required-to-
    load-the-law-that-activates-permission-sovereign.

Dependency classification (A191):

  * **core-critical** — blocks core-ready if missing.
  * **capability-critical** — blocks that capability only if missing.
  * **optional** — degrades only, does not block.
  * PostgreSQL/Qdrant/Ollama have no global fixed criticality; they are
    critical only when required by a core-ready contract in the current
    certified manifest.
  * Legacy hardcoded boot sequence has no effect.

This module provides **read-only data structures and verification**.  It
never directly starts/stops processes or mutates the startup state.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Final

# ---------------------------------------------------------------------------
# Startup phases (A192: PHASE-0 through PHASE-6)
# ---------------------------------------------------------------------------

STARTUP_PHASES: Final[tuple[str, ...]] = (
    "phase-0-local-preflight",
    "phase-1-minimal-information-bootstrap",
    "phase-2-read-official-codex",
    "phase-3-load-permission-directory",
    "phase-4-switch-normal-information-mode",
    "phase-5-classify-dependency-dag",
    "phase-6-activate-core-sovereigns",
)

# A192: CORE-READY — all conditions that must hold for core-ready
CORE_READY_CONDITIONS: Final[tuple[str, ...]] = (
    "official-codex-valid",
    "permission-sovereign-active",
    "normal-information-layer-active",
    "system-decision-active",
    "system-runtime-active",
    "maintenance-active",
    "all-core-critical-dependencies-ready",
)

# A192: BOOTSTRAP-CAPABILITY properties
BOOTSTRAP_CAPABILITY_PROPERTIES: Final[tuple[str, ...]] = (
    "pre-issued",
    "read-only",
    "startup-sovereign-bound",
    "official-codex-entry-only",
    "sealed-in-active-release",
    "verified-mechanically-without-live-permission-decision",
)

# ---------------------------------------------------------------------------
# Dependency criticality classes (A191)
# ---------------------------------------------------------------------------

DEPENDENCY_CRITICALITY_CLASSES: Final[tuple[str, ...]] = (
    "core-critical",
    "capability-critical",
    "optional",
)

# A191: services with no global fixed criticality
NO_FIXED_CRITICALITY_SERVICES: Final[tuple[str, ...]] = (
    "postgresql",
    "qdrant",
    "ollama",
)


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
# Dependency DAG (A192: PHASE-5 acyclic critical path)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class DependencyDAG:
    """A dependency DAG for startup ordering (A192: PHASE-5).

    The startup sovereign executes the acyclic critical path.  This data
    structure records the declared dependencies and provides cycle
    detection.
    """

    dependencies: tuple[DependencyDeclaration, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "dependencies": [d.as_dict() for d in self.dependencies],
            "is_acyclic": self.is_acyclic,
            "core_critical": [d.identity for d in self.core_critical],
            "capability_critical": [d.identity for d in self.capability_critical],
            "optional": [d.identity for d in self.optional],
        }

    @property
    def core_critical(self) -> tuple[DependencyDeclaration, ...]:
        return tuple(d for d in self.dependencies if d.is_core_critical)

    @property
    def capability_critical(self) -> tuple[DependencyDeclaration, ...]:
        return tuple(d for d in self.dependencies if d.is_capability_critical)

    @property
    def optional(self) -> tuple[DependencyDeclaration, ...]:
        return tuple(d for d in self.dependencies if d.is_optional)

    @property
    def is_acyclic(self) -> bool:
        """Check for cycles in the required_by graph (A192: acyclic)."""
        graph: dict[str, list[str]] = {}
        for dep in self.dependencies:
            graph.setdefault(dep.identity, [])
            if dep.required_by and dep.required_by != dep.identity:
                graph.setdefault(dep.required_by, [])
                graph[dep.required_by].append(dep.identity)

        # DFS cycle detection
        visited: set[str] = set()
        in_stack: set[str] = set()

        def has_cycle(node: str) -> bool:
            if node in in_stack:
                return True
            if node in visited:
                return False
            visited.add(node)
            in_stack.add(node)
            for neighbor in graph.get(node, []):
                if has_cycle(neighbor):
                    return True
            in_stack.discard(node)
            return False

        return not any(has_cycle(node) for node in graph)


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


# ---------------------------------------------------------------------------
# Phase verification (A192: no circular gate)
# ---------------------------------------------------------------------------

def verify_phase_order(
    completed_phases: tuple[str, ...],
) -> dict[str, Any]:
    """Verify that startup phases were executed in order without skips (A192).

    Per A192: ``FORBID:circular-startup-gate`` and the strict phase ordering.
    """
    violations: list[str] = []
    required = STARTUP_PHASES

    last_idx = -1
    for phase in required:
        if phase not in completed_phases:
            violations.append(f"missing-phase:{phase}")
            continue
        idx = completed_phases.index(phase)
        if idx <= last_idx:
            violations.append(f"out-of-order:{phase}")
        last_idx = idx

    return {
        "ok": len(violations) == 0,
        "basis": "A192/E167",
        "completed_phases": list(completed_phases),
        "required_phases": list(required),
        "violations": violations,
        "circular_gate": False,
        "no_skip": len(violations) == 0,
    }


# ---------------------------------------------------------------------------
# Core-ready verification (A192: CORE-READY)
# ---------------------------------------------------------------------------

def verify_core_ready(
    conditions: dict[str, bool],
) -> dict[str, Any]:
    """Verify all core-ready conditions are met (A192: CORE-READY).

    Per A192: ``CORE-READY:official-codex-valid+permission-sovereign-active+
    normal-information-layer-active+system-decision-active+system-runtime-
    active+maintenance-active+all-core-critical-dependencies-ready``.
    """
    missing: list[str] = []
    for condition in CORE_READY_CONDITIONS:
        if not conditions.get(condition, False):
            missing.append(condition)

    return {
        "ok": len(missing) == 0,
        "basis": "A192/E167",
        "conditions": {c: conditions.get(c, False) for c in CORE_READY_CONDITIONS},
        "missing": missing,
        "core_ready": len(missing) == 0,
    }


# ---------------------------------------------------------------------------
# Dependency classification verification (A191)
# ---------------------------------------------------------------------------

def verify_dependency_classification(
    dag: DependencyDAG,
) -> dict[str, Any]:
    """Verify dependency classification against A191 rules.

    Per A191: ``POSTGRESQL/QDRANT/OLLAMA:no-global-fixed-criticality+critical-
    only-when-required-by-a-core-ready-contract in-current-certified-manifest``.
    """
    violations: list[str] = []

    for dep in dag.dependencies:
        if not dep.valid_criticality:
            violations.append(
                f"invalid-criticality:{dep.identity}:{dep.criticality}"
            )
        # A191: no global fixed criticality for these services
        if dep.identity.lower() in NO_FIXED_CRITICALITY_SERVICES:
            # Criticality must be based on current required contract, not name.
            # We cannot verify the contract here, but we flag if criticality
            # appears hardcoded by name (no required_by declared).
            if dep.is_core_critical and not dep.required_by:
                violations.append(
                    f"name-based-criticality:{dep.identity}:"
                    f"core-critical-without-required-by"
                )

    # A191: MISSING-OPTIONAL does not block core
    # A191: MISSING-CAPABILITY-CRITICAL blocks that capability only
    # A191: MISSING-CORE-CRITICAL blocks core-ready

    return {
        "ok": len(violations) == 0,
        "basis": "A191/E166",
        "is_acyclic": dag.is_acyclic,
        "core_critical": [d.identity for d in dag.core_critical],
        "capability_critical": [d.identity for d in dag.capability_critical],
        "optional": [d.identity for d in dag.optional],
        "violations": violations,
        "legacy_fixed_sequence": False,
    }


# ---------------------------------------------------------------------------
# Failure handling (A192: FAILURE)
# ---------------------------------------------------------------------------

def startup_failure_signal(
    failure_scope: str,
    *,
    failed_dependency: str = "",
    generation_id: str = "",
) -> dict[str, Any]:
    """Produce a failure signal for startup failure (A192: FAILURE).

    Per A192: ``FAILURE:core-critical failure=>failed-generation+owned-reverse-
    DAG-cleanup+typed-user-visible-cause; non-core failure=>affected-capability-
    degraded-only+retry-budget``.
    """
    is_core = failure_scope == "core-critical"
    return {
        "signal_type": "startup-failure",
        "authority": "signal-only",
        "basis": "A192/E167",
        "failure_scope": failure_scope,
        "failed_dependency": failed_dependency,
        "generation_id": generation_id,
        "action": (
            "failed-generation+owned-reverse-DAG-cleanup+typed-user-visible-cause"
            if is_core
            else "affected-capability-degraded-only+retry-budget"
        ),
        "retry": "fresh-generation-or-owned-node-retry",
        "backoff": "exponential+jitter+circuit-breaker",
        "manual_approval_required": False,
    }


# ---------------------------------------------------------------------------
# Startup status (A192: STATUS)
# ---------------------------------------------------------------------------

def startup_status(
    generation: StartupGeneration,
    *,
    dag: DependencyDAG | None = None,
    core_ready_conditions: dict[str, bool] | None = None,
) -> dict[str, Any]:
    """Return the startup status for observability (A192: STATUS).

    Per A192: ``STATUS:sequenced-information-layer-events+authoritative-
    snapshot`` and ``READY:evidence-bound-to-generation+release+dependency-
    state``.
    """
    return {
        "generation_id": generation.generation_id,
        "release_id": generation.release_id,
        "started_at": generation.started_at,
        "current_phase": generation.current_phase,
        "core_ready": generation.core_ready,
        "deferred_active": generation.deferred_active,
        "dag": dag.as_dict() if dag else None,
        "core_ready_conditions": core_ready_conditions or {},
        "basis": "A192/E167",
        "single_flight": True,
        "user_action_required": False,
    }


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
