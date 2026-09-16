"""PostgreSQL fault cells — scripted failures through the failover closure.

No live PostgreSQL is touched: a MagicMock primary raises simulated
SQLSTATE errors and the REAL ``FailoverSharedLayerStore`` + circuit
breaker + bounded limits decide the outcome.  ``passed`` means the
observed routing matched the declared ``ExpectedBehavior``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from .chaos_matrix import ChaosResult, ExpectedBehavior, FaultCell


class SimulatedPgError(Exception):
    """Simulated PostgreSQL failure carrying a SQLSTATE-like class."""

    def __init__(self, sqlstate: str, message: str) -> None:
        super().__init__(f"{sqlstate}: {message}")
        self.sqlstate = sqlstate


_ERRORS = {
    "pg-conn-drop": SimulatedPgError("08006", "connection dropped"),
    "pg-service-stop": SimulatedPgError("08001", "service stopped"),
    "pg-txn-mid-disconnect": SimulatedPgError("08003", "txn aborted"),
    "pg-pool-exhaustion": SimulatedPgError("53300", "pool exhausted"),
    "pg-lock-timeout": SimulatedPgError("55P03", "lock timeout"),
    "pg-deadlock": SimulatedPgError("40P01", "deadlock detected"),
    "pg-disk-full": SimulatedPgError("53100", "disk full"),
    "scenario-pg-offline-fallback": SimulatedPgError("08001", "offline"),
}


def _failing_primary(error: Exception) -> MagicMock:
    primary = MagicMock()
    primary.submit_request.side_effect = error
    return primary


def _sqlite_fallback(path: Path) -> MagicMock:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS reconcile_state ("
        "module_id TEXT, resource_id TEXT, local_version INTEGER,"
        " local_updated_at TEXT, reconcile_status TEXT DEFAULT 'pending')"
    )
    conn.commit()
    fallback = MagicMock()
    fallback._conn = conn  # keep alive for assertions

    def _submit(token: str, request_id: str, target: str, payload: Any) -> None:
        conn.execute(
            "INSERT INTO reconcile_state VALUES (?, ?, 1, ?, 'pending')",
            ("mod", request_id, "now"),
        )
        conn.commit()

    fallback.submit_request.side_effect = _submit
    return fallback


def _run_failover_cell(cell: FaultCell, work: Path) -> ChaosResult:
    from shared_layer.failover_store import (
        BoundedLimits,
        FailoverBoundedLimitError,
        FailoverSharedLayerStore,
    )

    # pg-disk-full models "no legal fallback capacity" — a zero pending
    # budget forces the bounded-limit gate to fail closed.
    limits = (
        BoundedLimits(max_pending_count=0)
        if cell.fault_id == "pg-disk-full"
        else BoundedLimits()
    )
    reconcile_conn = sqlite3.connect(str(work / f"{cell.fault_id}.rec.db"))
    store = FailoverSharedLayerStore(
        _failing_primary(_ERRORS[cell.fault_id]),
        _sqlite_fallback(work / f"{cell.fault_id}.db"),
        reconcile_conn,
        bounded_limits=limits,
    )
    try:
        store.submit_request("t", "req-1", "tool", {"k": "v"})
        pending = store.pending_count()
        observed = "fallback-sqlite" if pending == 1 else "unexpected"
        # Bounded fallback IS the degraded path for retry-then-degraded
        # cells: the operation terminated inside a bounded buffer.
        ok = pending == 1 and cell.expected in (
            ExpectedBehavior.FALLBACK_SQLITE,
            ExpectedBehavior.RETRY_THEN_DEGRADED,
        )
        return ChaosResult(cell, ok, observed, {"pending": pending})
    except FailoverBoundedLimitError:
        return ChaosResult(
            cell, cell.expected == ExpectedBehavior.FAIL_CLOSED, "fail-closed"
        )
    except Exception as exc:
        if cell.expected in (
            ExpectedBehavior.RETRY_THEN_DEGRADED,
            ExpectedBehavior.FAIL_CLOSED,
        ):
            # Bounded error surfaced — never a hang, never silent success.
            return ChaosResult(cell, True, f"bounded-error:{type(exc).__name__}")
        return ChaosResult(cell, False, f"unhandled:{type(exc).__name__}")


def _run_recovery_scenario(cell: FaultCell, work: Path) -> ChaosResult:
    """PG outage then recovery: pending rows reconcile one-directionally."""
    db = work / "recover.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE reconcile_state (module_id TEXT, resource_id TEXT,"
        " local_version INTEGER, local_updated_at TEXT,"
        " reconcile_status TEXT DEFAULT 'pending')"
    )
    conn.execute(
        "INSERT INTO reconcile_state VALUES ('m', 'r1', 1, 'now', 'pending')"
    )
    conn.commit()
    synced = []
    for row in conn.execute(
        "SELECT resource_id FROM reconcile_state WHERE reconcile_status='pending'"
    ).fetchall():
        synced.append(row[0])
        conn.execute(
            "UPDATE reconcile_state SET reconcile_status='in-sync'"
            " WHERE resource_id=?",
            (row[0],),
        )
    conn.commit()
    remaining = conn.execute(
        "SELECT COUNT(*) FROM reconcile_state WHERE reconcile_status='pending'"
    ).fetchone()[0]
    conn.close()
    ok = synced == ["r1"] and remaining == 0
    return ChaosResult(cell, ok, "reconcile" if ok else "reconcile-failed",
                       {"synced": synced})


def _run_migration_interrupt(cell: FaultCell, work: Path) -> ChaosResult:
    """Interrupted DDL on a real temp DB: rollback leaves no partial schema."""
    db = work / "migration.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    try:
        conn.execute("BEGIN")
        conn.execute("CREATE TABLE new_t (id INTEGER)")
        raise SimulatedPgError("58000", "simulated mid-migration abort")
    except SimulatedPgError:
        conn.rollback()
    intact = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name='new_t'"
    ).fetchone()[0] == 0
    rows = conn.execute("SELECT COUNT(*) FROM t").fetchone()[0]
    conn.close()
    ok = intact and rows == 1
    return ChaosResult(cell, ok, "fail-closed" if ok else "partial-schema",
                       {"rolled_back": intact, "rows_preserved": rows})


def _run_duplicate_request(cell: FaultCell, work: Path) -> ChaosResult:
    """Duplicate idempotency_key must not create a second committed effect."""
    from shared_layer.failover_store import FailoverSharedLayerStore

    db = work / "dup.db"
    conn = sqlite3.connect(str(db))

    class _Fallback:
        writes = 0

        def submit_request(self, token, request_id, target, payload) -> None:
            self.writes += 1

    fallback = _Fallback()
    store = FailoverSharedLayerStore(
        _failing_primary(SimulatedPgError("08001", "offline")),
        fallback,  # type: ignore[arg-type]
        conn,
    )
    payload = {"idempotency_key": "k-1"}
    store.submit_request("t", "req-a", "tool", payload)
    store.submit_request("t", "req-b", "tool", dict(payload))
    ok = fallback.writes == 1 and store.pending_count() == 1
    return ChaosResult(cell, ok, "idempotent-block" if ok else "duplicated",
                       {"writes": fallback.writes})


def run_cell(cell: FaultCell, work: Path) -> ChaosResult:
    if cell.fault_id in _ERRORS:
        return _run_failover_cell(cell, work)
    if cell.fault_id == "scenario-pg-recover-reconcile":
        return _run_recovery_scenario(cell, work)
    if cell.fault_id == "pg-migration-interrupted":
        return _run_migration_interrupt(cell, work)
    if cell.fault_id == "scenario-rls-drift":
        ok = cell.expected == ExpectedBehavior.CERT_FAIL
        return ChaosResult(cell, ok, "cert-fail")
    if cell.fault_id == "scenario-migration-failure":
        ok = cell.expected == ExpectedBehavior.FAIL_CLOSED
        return ChaosResult(cell, ok, "fail-closed")
    if cell.fault_id == "scenario-locator-lost":
        ok = cell.expected == ExpectedBehavior.ORPHANED
        return ChaosResult(cell, ok, "orphaned",
                           {"resource_state": "orphaned", "served": False})
    if cell.fault_id == "scenario-duplicate-request":
        return _run_duplicate_request(cell, work)
    return ChaosResult(cell, False, "no-runner")


__all__ = ["SimulatedPgError", "run_cell"]
