"""Chaos Test Matrix (A365-A370, A499).

Formal, machine-readable fault-injection matrix for the three-engine data
platform.  Every fault cell declares:

    engine        postgresql | sqlite | qdrant
    fault_id      stable identifier used in evidence records
    injection     how the fault is introduced (controlled scope only)
    expected      the REQUIRED system behaviour — never "whatever happens"

A370 FORBID: fault injection against uncontrolled production scope.
Every executable cell runs against test doubles or temporary SQLite files
only; no cell may touch a live database.

Expected-behaviour vocabulary (A369 health states + recovery contract):
    FALLBACK_SQLITE   legal bounded SQLite fallback, reconcile pending
    RECONCILE         one-directional SQLite -> PostgreSQL reconcile
    DEGRADED          capability degraded, other engines unaffected
    FAIL_CLOSED       operation denied, evidence recorded, no mutation
    RETRY_THEN_DEGRADED  bounded retry with backoff, then degraded
    ORPHANED          resource marked orphaned, evidence preserved
    IDEMPOTENT_BLOCK  duplicate suppressed by idempotency key
    CERT_FAIL         startup/restore certification must FAIL
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


class ExpectedBehavior(str, Enum):
    FALLBACK_SQLITE = "fallback-sqlite"
    RECONCILE = "reconcile"
    DEGRADED = "degraded"
    FAIL_CLOSED = "fail-closed"
    RETRY_THEN_DEGRADED = "retry-then-degraded"
    ORPHANED = "orphaned"
    IDEMPOTENT_BLOCK = "idempotent-block"
    CERT_FAIL = "cert-fail"


@dataclass(frozen=True)
class FaultCell:
    """One row of the formal chaos matrix."""

    fault_id: str
    engine: str
    fault: str
    injection: str
    expected: ExpectedBehavior
    verifies: str  # what "correct" means, stated verifiably


@dataclass
class ChaosResult:
    """Executed result for one fault cell."""

    cell: FaultCell
    passed: bool
    observed: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChaosReport:
    """Aggregated matrix run."""

    total: int = 0
    passed: int = 0
    failed: int = 0
    results: list[ChaosResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.failed == 0


# ---------------------------------------------------------------------------
# The formal matrix — single source of truth for fault expectations.
# ---------------------------------------------------------------------------

_POSTGRESQL_CELLS: tuple[FaultCell, ...] = (
    FaultCell(
        "pg-conn-drop", "postgresql", "connection drop mid-operation",
        "primary raises OperationalError on execute",
        ExpectedBehavior.FALLBACK_SQLITE,
        "write routed to bounded SQLite fallback and marked pending",
    ),
    FaultCell(
        "pg-service-stop", "postgresql", "service stopped",
        "primary raises ConnectionError on every call",
        ExpectedBehavior.FALLBACK_SQLITE,
        "circuit opens; bounded fallback; SQLite never promotes to authority",
    ),
    FaultCell(
        "pg-txn-mid-disconnect", "postgresql", "disconnect mid-transaction",
        "primary raises after BEGIN before COMMIT",
        ExpectedBehavior.FALLBACK_SQLITE,
        "no partial commit visible; fallback records pending with correlation",
    ),
    FaultCell(
        "pg-pool-exhaustion", "postgresql", "connection pool exhausted",
        "pool acquire raises PoolTimeout",
        ExpectedBehavior.RETRY_THEN_DEGRADED,
        "bounded wait then degraded; one workload cannot exhaust others",
    ),
    FaultCell(
        "pg-lock-timeout", "postgresql", "lock_timeout exceeded",
        "primary raises LockTimeout (55P03)",
        ExpectedBehavior.RETRY_THEN_DEGRADED,
        "bounded retry with jitter then bounded fallback/dead-letter",
    ),
    FaultCell(
        "pg-deadlock", "postgresql", "deadlock detected",
        "primary raises DeadlockDetected (40P01)",
        ExpectedBehavior.RETRY_THEN_DEGRADED,
        "bounded serialization retry; never duplicates committed effects",
    ),
    FaultCell(
        "pg-disk-full", "postgresql", "disk full (simulated)",
        "primary raises DiskFull (53100/53200 class)",
        ExpectedBehavior.FAIL_CLOSED,
        "writes denied; evidence recorded; no silent data loss",
    ),
    FaultCell(
        "pg-migration-interrupted", "postgresql", "migration aborts mid-run",
        "migration executor raises partway; connection dies",
        ExpectedBehavior.FAIL_CLOSED,
        "transactional rollback or fail-closed; schema head unchanged",
    ),
)

_SQLITE_CELLS: tuple[FaultCell, ...] = (
    FaultCell(
        "sqlite-locked", "sqlite", "database locked",
        "second writer holds exclusive lock on temp DB",
        ExpectedBehavior.RETRY_THEN_DEGRADED,
        "busy_timeout bounded retry, then degraded/fail-closed",
    ),
    FaultCell(
        "sqlite-wal-oversize", "sqlite", "WAL file over budget",
        "grow -wal file beyond max_wal_size_bytes",
        ExpectedBehavior.FAIL_CLOSED,
        "bounded-limit check raises FailoverBoundedLimitError",
    ),
    FaultCell(
        "sqlite-busy-timeout", "sqlite", "busy timeout under contention",
        "concurrent writer forces SQLITE_BUSY until timeout",
        ExpectedBehavior.RETRY_THEN_DEGRADED,
        "bounded busy wait; operation never silently lost",
    ),
    FaultCell(
        "sqlite-readonly", "sqlite", "database file read-only",
        "chmod read-only on temp DB",
        ExpectedBehavior.FAIL_CLOSED,
        "write denied with error; pending work stays queued, not lost",
    ),
    FaultCell(
        "sqlite-schema-version-drift", "sqlite", "schema_version mismatch",
        "corrupt schema_version row in temp DB",
        ExpectedBehavior.CERT_FAIL,
        "startup certification refuses write-ready state",
    ),
    FaultCell(
        "sqlite-partial-corruption", "sqlite", "partial data corruption",
        "flip bytes mid-file of temp DB",
        ExpectedBehavior.CERT_FAIL,
        "integrity check detects corruption; fail-closed, no overwrite",
    ),
    FaultCell(
        "sqlite-reconcile-backlog", "sqlite", "reconcile backlog overflow",
        "seed pending rows beyond max_pending_count",
        ExpectedBehavior.FAIL_CLOSED,
        "fail-closed on bounded limit; backlog never silently dropped",
    ),
)

_QDRANT_CELLS: tuple[FaultCell, ...] = (
    FaultCell(
        "qdrant-offline", "qdrant", "service offline",
        "client raises ConnectionError on every call",
        ExpectedBehavior.DEGRADED,
        "RAG semantic capability degraded; PostgreSQL unaffected",
    ),
    FaultCell(
        "qdrant-point-missing", "qdrant", "referenced point missing",
        "index_state row references absent qdrant_point_id",
        ExpectedBehavior.DEGRADED,
        "mismatch detected; marked stale/reconcile_required, not served",
    ),
    FaultCell(
        "qdrant-stale-vector", "qdrant", "stale vector (old generation)",
        "vector carries prior-generation source_revision",
        ExpectedBehavior.DEGRADED,
        "stale vector never served as current (A370 generation fence)",
    ),
    FaultCell(
        "qdrant-collection-missing", "qdrant", "collection missing",
        "expected collection absent on query",
        ExpectedBehavior.DEGRADED,
        "degraded semantic capability; no crash; rebuild cert required",
    ),
    FaultCell(
        "qdrant-chunk-no-vector", "qdrant", "PG chunk exists, vector absent",
        "chunk row in PostgreSQL without matching point",
        ExpectedBehavior.RECONCILE,
        "detected as reconcile_required; vector rebuilt from canonical data",
    ),
    FaultCell(
        "qdrant-version-mismatch", "qdrant", "vector version != resource revision",
        "point version drifts from resource_metadata.version",
        ExpectedBehavior.DEGRADED,
        "consistency check flags stale; authoritative use blocked",
    ),
)

# Named end-to-end scenarios (the user's explicit matrix rows).
_SCENARIO_CELLS: tuple[FaultCell, ...] = (
    FaultCell(
        "scenario-pg-offline-fallback", "postgresql",
        "PostgreSQL offline under normal load",
        "all primary calls fail", ExpectedBehavior.FALLBACK_SQLITE,
        "legal bounded SQLite fallback engaged",
    ),
    FaultCell(
        "scenario-pg-recover-reconcile", "postgresql",
        "PostgreSQL recovers after outage",
        "primary heals after degraded window",
        ExpectedBehavior.RECONCILE,
        "one-directional SQLite -> PG reconcile of pending rows",
    ),
    FaultCell(
        "scenario-sqlite-locked-retry", "sqlite",
        "SQLite locked during reconcile",
        "exclusive lock held during pending write",
        ExpectedBehavior.RETRY_THEN_DEGRADED,
        "bounded retry then degraded; never silent drop",
    ),
    FaultCell(
        "scenario-qdrant-offline", "qdrant",
        "Qdrant offline during RAG query",
        "client unreachable", ExpectedBehavior.DEGRADED,
        "semantic degraded; zero PostgreSQL pollution",
    ),
    FaultCell(
        "scenario-migration-failure", "postgresql",
        "migration failure mid-apply",
        "DDL error inside migration",
        ExpectedBehavior.FAIL_CLOSED,
        "rollback / fail-closed; no partial schema accepted",
    ),
    FaultCell(
        "scenario-rls-drift", "postgresql",
        "RLS policy drift detected at startup",
        "catalog shows dropped/disabled policy",
        ExpectedBehavior.CERT_FAIL,
        "startup certification fails before write-ready",
    ),
    FaultCell(
        "scenario-locator-lost", "postgresql",
        "locator record missing for live resource",
        "locator_id lookup returns nothing",
        ExpectedBehavior.ORPHANED,
        "resource marked orphaned; evidence preserved; never served",
    ),
    FaultCell(
        "scenario-duplicate-request", "postgresql",
        "duplicate request delivery",
        "same idempotency_key submitted twice",
        ExpectedBehavior.IDEMPOTENT_BLOCK,
        "second submission suppressed; single committed side effect",
    ),
)

CHAOS_MATRIX: tuple[FaultCell, ...] = (
    _POSTGRESQL_CELLS + _SQLITE_CELLS + _QDRANT_CELLS + _SCENARIO_CELLS
)


def matrix_for(engine: str) -> tuple[FaultCell, ...]:
    return tuple(c for c in CHAOS_MATRIX if c.engine == engine)


def matrix_digest() -> str:
    """Stable digest of the declared matrix for certification evidence."""
    import hashlib

    digest = hashlib.sha256()
    for cell in CHAOS_MATRIX:
        digest.update(
            f"{cell.fault_id}:{cell.expected.value}\n".encode("utf-8")
        )
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Execution harness — cells run against injected doubles only.
# ---------------------------------------------------------------------------

# A runner takes a FaultCell and returns ChaosResult.  Runners are supplied
# by the test/verification layer so the matrix itself stays declarative and
# free of any live-infrastructure dependency.
CellRunner = Callable[[FaultCell], ChaosResult]


def run_matrix(
    runner: CellRunner,
    *,
    engines: tuple[str, ...] | None = None,
) -> ChaosReport:
    """Execute every cell through ``runner`` and aggregate PASS/FAIL."""
    cells = (
        CHAOS_MATRIX
        if engines is None
        else tuple(c for c in CHAOS_MATRIX if c.engine in engines)
    )
    report = ChaosReport(total=len(cells))
    for cell in cells:
        try:
            result = runner(cell)
        except Exception as exc:
            result = ChaosResult(cell, False, observed=f"runner-error: {exc}")
        report.results.append(result)
        if result.passed:
            report.passed += 1
        else:
            report.failed += 1
    return report


__all__ = [
    "CHAOS_MATRIX",
    "CellRunner",
    "ChaosReport",
    "ChaosResult",
    "ExpectedBehavior",
    "FaultCell",
    "matrix_digest",
    "matrix_for",
    "run_matrix",
]
