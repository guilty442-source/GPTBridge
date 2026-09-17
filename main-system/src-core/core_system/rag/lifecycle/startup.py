"""Startup gate + Canonical Takeover acceptance.

Startup:
    load policy -> check PG -> check Qdrant -> check embedding
    runtime -> check active generation -> check schema versions
    -> check migration state
      => CANONICAL_READY | DEGRADED_READY | BLOCKED

BLOCKED (never hard-start): schema incompatible, vector dimension
mismatch, active generation missing, migration half-complete.

Takeover is done only when the 12 acceptance criteria pass — and
afterwards an invariant forbids degraded-as-default while canonical
is healthy (irreversible switch).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class StartupVerdict(str, Enum):
    CANONICAL_READY = "CANONICAL_READY"
    DEGRADED_READY = "DEGRADED_READY"
    BLOCKED = "BLOCKED"


@dataclass(frozen=True, slots=True)
class StartupChecks:
    policy_loaded: bool = True
    pg_ok: bool = True
    qdrant_ok: bool = True
    embedding_runtime_ok: bool = True
    active_generation_present: bool = True
    schema_compatible: bool = True
    migration_complete: bool = True
    vector_dimension_match: bool = True


def startup_gate(checks: StartupChecks) -> tuple[StartupVerdict, tuple[str, ...]]:
    """Hard blockers first; a merely-unreachable Qdrant degrades,
    an inconsistent system never starts."""
    blocked: list[str] = []
    if not checks.policy_loaded:
        blocked.append("policy-not-loaded")
    if not checks.schema_compatible:
        blocked.append("schema-incompatible")
    if not checks.vector_dimension_match:
        blocked.append("vector-dimension-mismatch")
    if not checks.active_generation_present:
        blocked.append("active-generation-missing")
    if not checks.migration_complete:
        blocked.append("migration-half-complete")
    if blocked:
        return StartupVerdict.BLOCKED, tuple(blocked)

    degraded: list[str] = []
    if not checks.pg_ok:
        degraded.append("pg-unreachable")
    if not checks.qdrant_ok:
        degraded.append("qdrant-unreachable")
    if not checks.embedding_runtime_ok:
        degraded.append("embedding-runtime-down")
    if degraded:
        return StartupVerdict.DEGRADED_READY, tuple(degraded)
    return StartupVerdict.CANONICAL_READY, ()


# ----------------------------------------------------------------------
# Canonical Takeover acceptance
# ----------------------------------------------------------------------
TAKEOVER_CRITERIA: tuple[str, ...] = (
    "local-rag-query-via-canonical-gateway",
    "qdrant-is-dense-primary-path",
    "pg-is-metadata-fts-index-state-primary",
    "sqlite-fault-only",
    "canonical-true-responses",
    "reconciliation-required-false",
    "qdrant-down-enters-degraded",
    "qdrant-recovered-enters-reconciling",
    "reconcile-complete-returns-canonical",
    "benchmark-pass",
    "authorization-tests-pass",
    "restart-tests-pass",
)


@dataclass(frozen=True, slots=True)
class TakeoverReport:
    results: dict[str, bool]
    complete: bool
    missing: tuple[str, ...]


def evaluate_takeover(results: dict[str, bool]) -> TakeoverReport:
    """CANONICAL_TAKEOVER_COMPLETE only when all 12 criteria pass —
    'Qdrant is reachable' is not a completion condition."""
    missing = tuple(
        c for c in TAKEOVER_CRITERIA if not results.get(c, False)
    )
    unknown = tuple(k for k in results if k not in TAKEOVER_CRITERIA)
    return TakeoverReport(
        results=dict(results),
        complete=not missing and not unknown,
        missing=missing + tuple(f"unknown:{u}" for u in unknown),
    )


def select_backend(
    canonical_healthy: bool, takeover_complete: bool
) -> str:
    """Post-takeover invariant: while canonical services are healthy
    the degraded backend MUST NOT be selected — no refactor may ever
    revert SQLite to the normal path."""
    if takeover_complete:
        return "canonical" if canonical_healthy else "degraded"
    return "degraded" if not canonical_healthy else "canonical"


__all__ = [
    "StartupChecks",
    "StartupVerdict",
    "TAKEOVER_CRITERIA",
    "TakeoverReport",
    "evaluate_takeover",
    "select_backend",
    "startup_gate",
]
