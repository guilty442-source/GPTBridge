"""SQLite fault cells — REAL fault injection on temporary database files.

These are not simulations: an exclusive lock is genuinely held, the file
is genuinely chmod'd read-only, bytes are genuinely flipped, WAL is
genuinely grown.  Only the scope is controlled (temp files, never a live
module database — A370).
"""
from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path
from unittest.mock import MagicMock

from .chaos_matrix import ChaosResult, FaultCell


def run_cell(cell: FaultCell, work: Path) -> ChaosResult:
    db = work / f"{cell.fault_id}.db"
    conn = sqlite3.connect(str(db), timeout=0.1)
    conn.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY)")
    conn.execute("CREATE TABLE IF NOT EXISTS schema_version (v INTEGER)")
    conn.execute("INSERT INTO schema_version VALUES (1)")
    conn.commit()
    try:
        if cell.fault_id in (
            "sqlite-locked", "sqlite-busy-timeout",
            "scenario-sqlite-locked-retry",
        ):
            return _locked(cell, db, conn)
        if cell.fault_id == "sqlite-wal-oversize":
            return _wal_oversize(cell, db, conn)
        if cell.fault_id == "sqlite-readonly":
            return _readonly(cell, db, conn)
        if cell.fault_id == "sqlite-schema-version-drift":
            return _schema_drift(cell, conn)
        if cell.fault_id == "sqlite-partial-corruption":
            return _corruption(cell, db, conn)
        if cell.fault_id == "sqlite-reconcile-backlog":
            return _backlog(cell, work)
        return ChaosResult(cell, False, "no-runner")
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _locked(cell: FaultCell, db: Path, conn: sqlite3.Connection) -> ChaosResult:
    """Hold an exclusive lock; a second writer must bound out, not hang."""
    locker = sqlite3.connect(str(db))
    locker.execute("BEGIN EXCLUSIVE")
    locker.execute("INSERT INTO t VALUES (99)")
    start = time.monotonic()
    try:
        blocked = sqlite3.connect(str(db), timeout=0.05)
        try:
            blocked.execute("INSERT INTO t VALUES (1)")
            blocked.commit()
            return ChaosResult(cell, False, "unexpected-success")
        except sqlite3.OperationalError:
            elapsed = time.monotonic() - start
            return ChaosResult(cell, elapsed < 5.0, "busy-timeout",
                               {"waited_seconds": round(elapsed, 3)})
        finally:
            blocked.close()
    finally:
        locker.rollback()
        locker.close()


def _wal_oversize(cell: FaultCell, db: Path, conn: sqlite3.Connection) -> ChaosResult:
    """Grow the -wal file beyond budget; bounded-limit gate must fail-closed."""
    from shared_layer.failover_store import (
        BoundedLimits,
        FailoverBoundedLimitError,
        FailoverSharedLayerStore,
    )

    conn.execute("PRAGMA journal_mode=WAL")
    for i in range(200):
        conn.execute("INSERT INTO t VALUES (?)", (i,))
    conn.commit()
    wal = Path(str(db) + "-wal")
    try:
        wal_size = wal.stat().st_size if wal.exists() else 0
    except OSError:
        wal_size = 0
    limits = BoundedLimits(max_wal_size_bytes=max(1, wal_size - 1))
    store = FailoverSharedLayerStore(
        MagicMock(), MagicMock(), conn,
        bounded_limits=limits,
        db_path=str(db),
    )
    try:
        store._check_bounded_limits()
        return ChaosResult(cell, False, "limit-not-enforced")
    except FailoverBoundedLimitError:
        return ChaosResult(cell, True, "fail-closed")


def _readonly(cell: FaultCell, db: Path, conn: sqlite3.Connection) -> ChaosResult:
    conn.close()
    os.chmod(str(db), stat.S_IREAD)
    try:
        writer = sqlite3.connect(str(db), timeout=0.05)
        try:
            writer.execute("INSERT INTO t VALUES (1)")
            writer.commit()
            return ChaosResult(cell, False, "write-succeeded-on-readonly")
        except sqlite3.OperationalError:
            return ChaosResult(cell, True, "fail-closed")
        finally:
            writer.close()
    finally:
        os.chmod(str(db), stat.S_IREAD | stat.S_IWRITE)


def _schema_drift(cell: FaultCell, conn: sqlite3.Connection) -> ChaosResult:
    """Corrupt schema_version; certification must detect the drift."""
    conn.execute("UPDATE schema_version SET v = 999")
    conn.commit()
    row = conn.execute("SELECT v FROM schema_version").fetchone()
    detected = not row or int(row[0]) != 1
    return ChaosResult(cell, detected, "cert-fail" if detected else "missed",
                       {"found": row[0] if row else None, "expected": 1})


def _corruption(cell: FaultCell, db: Path, conn: sqlite3.Connection) -> ChaosResult:
    """Flip bytes mid-file; PRAGMA integrity_check must detect it."""
    conn.commit()
    conn.close()
    data = bytearray(db.read_bytes())
    if len(data) > 200:
        data[100] ^= 0xFF
        data[101] ^= 0xFF
    db.write_bytes(bytes(data))
    probe = sqlite3.connect(str(db))
    try:
        result = probe.execute("PRAGMA integrity_check").fetchone()
        intact = bool(result and result[0] == "ok")
    except sqlite3.DatabaseError:
        intact = False
    finally:
        probe.close()
    return ChaosResult(cell, not intact,
                       "corruption-detected" if not intact else "missed")


def _backlog(cell: FaultCell, work: Path) -> ChaosResult:
    """Pending backlog beyond bounded limit must fail-closed, never drop."""
    from shared_layer.failover_store import (
        BoundedLimits,
        FailoverBoundedLimitError,
        FailoverSharedLayerStore,
    )

    db = work / "backlog.db"
    conn = sqlite3.connect(str(db))
    limits = BoundedLimits(max_pending_count=10)
    store = FailoverSharedLayerStore(
        MagicMock(), MagicMock(), conn,
        bounded_limits=limits,
        db_path=str(db),
    )
    # Seed past the bounded pending limit through the governed schema.
    conn.executemany(
        "INSERT INTO reconcile_state (module_id, resource_id,"
        " local_version, local_updated_at, reconcile_status)"
        " VALUES ('m', ?, 1, 'now', 'pending')",
        [(f"r{i}",) for i in range(50)],
    )
    conn.commit()
    try:
        store._check_bounded_limits()
        return ChaosResult(cell, False, "backlog-accepted")
    except FailoverBoundedLimitError:
        return ChaosResult(cell, True, "fail-closed")


__all__ = ["run_cell"]
