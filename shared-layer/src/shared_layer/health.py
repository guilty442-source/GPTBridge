"""Health decomposition — dependency vs. capability (A44/E30).

Splits "PostgreSQL = online" into granular checks so the failover
controller and audit pipeline can distinguish:

  dependency checks (is the substrate reachable?):
    - connection
    - pool
    - schema_version
    - migration

  capability checks (can we actually use it?):
    - RLS
    - transport
    - reconcile
    - rag_metadata

Each check returns a ``HealthState`` (healthy / degraded / failed) plus a
short diagnostic string.  The composite ``health_snapshot()`` aggregates
all checks into a single report suitable for audit logging.
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

_logger = logging.getLogger("gptbridge.health")


class HealthState(str, enum.Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass(frozen=True)
class HealthCheckResult:
    """Result of a single dependency or capability check."""

    name: str
    state: HealthState
    detail: str = ""
    latency_ms: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def healthy(self) -> bool:
        return self.state == HealthState.HEALTHY


class HealthChecker:
    """Decomposed health checker for the shared-layer substrate.

    Each check is a callable that returns a ``HealthCheckResult``.
    Checks are registered at construction and re-run on each
    ``health_snapshot()`` call.
    """

    def __init__(self) -> None:
        self._checks: dict[str, Callable[[], HealthCheckResult]] = {}
        self._lock = threading.RLock()
        self._last_snapshot: dict[str, HealthCheckResult] = {}
        self._last_snapshot_at: float = 0.0

    def register(self, name: str, check: Callable[[], HealthCheckResult]) -> None:
        with self._lock:
            self._checks[name] = check

    def health_snapshot(self) -> dict[str, Any]:
        """Run all registered checks and return a composite snapshot."""
        results: dict[str, HealthCheckResult] = {}
        with self._lock:
            checks = dict(self._checks)
        for name, check in checks.items():
            start = time.monotonic()
            try:
                result = check()
            except Exception as exc:
                result = HealthCheckResult(
                    name=name,
                    state=HealthState.FAILED,
                    detail=f"check-error: {exc}",
                    latency_ms=(time.monotonic() - start) * 1000,
                )
            results[name] = result
        with self._lock:
            self._last_snapshot = results
            self._last_snapshot_at = time.time()
        # Composite state: failed if any failed, degraded if any degraded, else healthy.
        states = [r.state for r in results.values()]
        if HealthState.FAILED in states:
            composite = HealthState.FAILED
        elif HealthState.DEGRADED in states:
            composite = HealthState.DEGRADED
        else:
            composite = HealthState.HEALTHY
        return {
            "composite": composite.value,
            "checks": {
                name: {
                    "state": r.state.value,
                    "detail": r.detail,
                    "latency_ms": round(r.latency_ms, 2),
                    "metadata": r.metadata,
                }
                for name, r in results.items()
            },
            "checked_at": self._last_snapshot_at,
        }

    @property
    def last_snapshot(self) -> dict[str, HealthCheckResult]:
        with self._lock:
            return dict(self._last_snapshot)


# ---------------------------------------------------------------------------
# Built-in check factories
# ---------------------------------------------------------------------------


def check_connection(connect_fn: Callable[[], Any]) -> HealthCheckResult:
    """Dependency: can we acquire a connection?"""
    start = time.monotonic()
    try:
        conn = connect_fn()
        latency = (time.monotonic() - start) * 1000
        if conn is None:
            return HealthCheckResult(
                "connection", HealthState.FAILED, "connect returned None", latency
            )
        # Best-effort close
        try:
            close = getattr(conn, "close", None)
            if callable(close):
                close()
        except Exception:
            pass
        return HealthCheckResult(
            "connection", HealthState.HEALTHY, "ok", latency
        )
    except Exception as exc:
        return HealthCheckResult(
            "connection", HealthState.FAILED, f"connect-error: {exc}", (time.monotonic() - start) * 1000
        )


def check_pool(pool_stats_fn: Callable[[], dict[str, Any]]) -> HealthCheckResult:
    """Dependency: is the connection pool usable?"""
    start = time.monotonic()
    try:
        stats = pool_stats_fn()
        latency = (time.monotonic() - start) * 1000
        in_use = int(stats.get("in_use", 0))
        max_size = int(stats.get("max_size", 0))
        if max_size == 0:
            return HealthCheckResult(
                "pool", HealthState.FAILED, "pool not initialized", latency
            )
        utilization = in_use / max_size if max_size else 0.0
        if utilization >= 0.95:
            return HealthCheckResult(
                "pool", HealthState.DEGRADED,
                f"pool near exhaustion: {in_use}/{max_size}",
                latency,
                {"in_use": in_use, "max_size": max_size, "utilization": utilization},
            )
        return HealthCheckResult(
            "pool", HealthState.HEALTHY,
            f"ok: {in_use}/{max_size} in use",
            latency,
            {"in_use": in_use, "max_size": max_size, "utilization": utilization},
        )
    except Exception as exc:
        return HealthCheckResult(
            "pool", HealthState.FAILED, f"pool-error: {exc}", (time.monotonic() - start) * 1000
        )


def check_schema_version(
    query_fn: Callable[[], int], expected: int
) -> HealthCheckResult:
    """Dependency: does the schema version match the expected value?"""
    start = time.monotonic()
    try:
        actual = query_fn()
        latency = (time.monotonic() - start) * 1000
        if actual == expected:
            return HealthCheckResult(
                "schema_version", HealthState.HEALTHY,
                f"ok: version={actual}", latency, {"version": actual}
            )
        return HealthCheckResult(
            "schema_version", HealthState.DEGRADED,
            f"mismatch: expected={expected}, actual={actual}",
            latency, {"expected": expected, "actual": actual}
        )
    except Exception as exc:
        return HealthCheckResult(
            "schema_version", HealthState.FAILED,
            f"query-error: {exc}", (time.monotonic() - start) * 1000
        )


def check_migration(
    migration_fn: Callable[[], bool]
) -> HealthCheckResult:
    """Dependency: are all migrations applied?"""
    start = time.monotonic()
    try:
        applied = migration_fn()
        latency = (time.monotonic() - start) * 1000
        if applied:
            return HealthCheckResult(
                "migration", HealthState.HEALTHY, "all migrations applied", latency
            )
        return HealthCheckResult(
            "migration", HealthState.DEGRADED, "pending migrations", latency
        )
    except Exception as exc:
        return HealthCheckResult(
            "migration", HealthState.FAILED, f"migration-error: {exc}", (time.monotonic() - start) * 1000
        )


def check_rls(rls_fn: Callable[[], bool]) -> HealthCheckResult:
    """Capability: is Row Level Security enabled on critical tables?"""
    start = time.monotonic()
    try:
        enabled = rls_fn()
        latency = (time.monotonic() - start) * 1000
        if enabled:
            return HealthCheckResult(
                "rls", HealthState.HEALTHY, "RLS enabled", latency
            )
        return HealthCheckResult(
            "rls", HealthState.FAILED, "RLS not enabled on critical tables", latency
        )
    except Exception as exc:
        return HealthCheckResult(
            "rls", HealthState.FAILED, f"rls-error: {exc}", (time.monotonic() - start) * 1000
        )


def check_transport(transport_fn: Callable[[], bool]) -> HealthCheckResult:
    """Capability: can we read/write the transport table?"""
    start = time.monotonic()
    try:
        ok = transport_fn()
        latency = (time.monotonic() - start) * 1000
        if ok:
            return HealthCheckResult(
                "transport", HealthState.HEALTHY, "transport read/write ok", latency
            )
        return HealthCheckResult(
            "transport", HealthState.DEGRADED, "transport read/write degraded", latency
        )
    except Exception as exc:
        return HealthCheckResult(
            "transport", HealthState.FAILED, f"transport-error: {exc}", (time.monotonic() - start) * 1000
        )


def check_reconcile(
    pending_count_fn: Callable[[], int],
    *,
    degraded_threshold: int = 100,
    failed_threshold: int = 10000,
) -> HealthCheckResult:
    """Capability: is the reconciliation queue in a healthy state?"""
    start = time.monotonic()
    try:
        pending = pending_count_fn()
        latency = (time.monotonic() - start) * 1000
        if pending >= failed_threshold:
            return HealthCheckResult(
                "reconcile", HealthState.FAILED,
                f"reconcile queue overflow: {pending} pending",
                latency, {"pending": pending}
            )
        if pending >= degraded_threshold:
            return HealthCheckResult(
                "reconcile", HealthState.DEGRADED,
                f"reconcile queue growing: {pending} pending",
                latency, {"pending": pending}
            )
        return HealthCheckResult(
            "reconcile", HealthState.HEALTHY,
            f"ok: {pending} pending", latency, {"pending": pending}
        )
    except Exception as exc:
        return HealthCheckResult(
            "reconcile", HealthState.FAILED,
            f"reconcile-error: {exc}", (time.monotonic() - start) * 1000
        )


def check_rag_metadata(rag_fn: Callable[[], bool]) -> HealthCheckResult:
    """Capability: can we read the RAG metadata authority tables?"""
    start = time.monotonic()
    try:
        ok = rag_fn()
        latency = (time.monotonic() - start) * 1000
        if ok:
            return HealthCheckResult(
                "rag_metadata", HealthState.HEALTHY, "RAG metadata readable", latency
            )
        return HealthCheckResult(
            "rag_metadata", HealthState.DEGRADED, "RAG metadata degraded", latency
        )
    except Exception as exc:
        return HealthCheckResult(
            "rag_metadata", HealthState.FAILED,
            f"rag-metadata-error: {exc}", (time.monotonic() - start) * 1000
        )


def build_default_health_checker(
    *,
    connect_fn: Optional[Callable[[], Any]] = None,
    pool_stats_fn: Optional[Callable[[], dict[str, Any]]] = None,
    schema_version_fn: Optional[Callable[[], int]] = None,
    expected_schema_version: int = 1,
    migration_fn: Optional[Callable[[], bool]] = None,
    rls_fn: Optional[Callable[[], bool]] = None,
    transport_fn: Optional[Callable[[], bool]] = None,
    pending_count_fn: Optional[Callable[[], int]] = None,
    rag_metadata_fn: Optional[Callable[[], bool]] = None,
) -> HealthChecker:
    """Build a HealthChecker with the standard 8 checks wired."""
    checker = HealthChecker()
    if connect_fn:
        checker.register("connection", lambda: check_connection(connect_fn))
    if pool_stats_fn:
        checker.register("pool", lambda: check_pool(pool_stats_fn))
    if schema_version_fn:
        checker.register(
            "schema_version",
            lambda: check_schema_version(schema_version_fn, expected_schema_version),
        )
    if migration_fn:
        checker.register("migration", lambda: check_migration(migration_fn))
    if rls_fn:
        checker.register("rls", lambda: check_rls(rls_fn))
    if transport_fn:
        checker.register("transport", lambda: check_transport(transport_fn))
    if pending_count_fn:
        checker.register("reconcile", lambda: check_reconcile(pending_count_fn))
    if rag_metadata_fn:
        checker.register("rag_metadata", lambda: check_rag_metadata(rag_metadata_fn))
    return checker


__all__ = [
    "HealthState",
    "HealthCheckResult",
    "HealthChecker",
    "check_connection",
    "check_pool",
    "check_schema_version",
    "check_migration",
    "check_rls",
    "check_transport",
    "check_reconcile",
    "check_rag_metadata",
    "build_default_health_checker",
]
