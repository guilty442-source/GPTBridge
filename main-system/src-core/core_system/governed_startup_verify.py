"""Deadlock-free governed startup — verification (A192/E167, A191/E166).

Verification functions and the dependency DAG data structure.  Split from
``governed_startup`` for A185/E160 source-size compliance.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from core_system.governed_startup_types import (
    CORE_READY_CONDITIONS,
    DEPENDENCY_CRITICALITY_CLASSES,
    NO_FIXED_CRITICALITY_SERVICES,
    STARTUP_PHASES,
    DependencyDeclaration,
)

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
    normal-information-layer-active+decision-active+system-runtime-
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
