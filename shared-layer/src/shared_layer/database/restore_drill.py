"""Restore Drill (A365 FORBID: backup declared successful without
restore/reconcile proof; A369 RECOVERY-STATE-MACHINE).

A backup is not evidence — only a verified restore is.  The drill is:

    Backup
      -> simulated database loss
        -> Restore
          -> schema verification        (schema_version + tables)
          -> migration head verification
          -> role / RLS verification
          -> audit head verification    (audit_sequence continuity)
          -> reconcile                  (backlog drained or bounded)
          -> Qdrant linkage verification
          -> READY                      (only if every check passed)

Verification MUST cover at least: schema_version, resource_count,
relation_count, audit_sequence, transport state, RLS, roles,
locator integrity, reconcile backlog, Qdrant references.

The drill runs through a ``DrillAdapter`` so the same contract verifies a
real PostgreSQL engine and the test-scope SQLite adapter — a green drill
against a fake proves the harness, not the backup.  ``READY`` requires
the adapter to report ``engine_live=True`` for production certification.
"""
from __future__ import annotations

import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class DrillCheck:
    """One verification step in the restore chain."""

    name: str
    passed: bool
    detail: str = ""


@dataclass
class RestoreDrillResult:
    """Full drill outcome — READY only when every check passed."""

    ready: bool
    checks: list[DrillCheck] = field(default_factory=list)
    duration_seconds: float = 0.0
    engine_live: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "engine_live": self.engine_live,
            "duration_seconds": round(self.duration_seconds, 3),
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail}
                for c in self.checks
            ],
        }


class DrillEngineUnavailable(RuntimeError):
    """Raised when a drill step needs a live engine that is unavailable."""


class DrillAdapter(Protocol):
    """Engine-specific operations the drill orchestrates."""

    engine_live: bool  # True only for a real engine, False for test fakes

    def backup(self, destination: Path) -> None: ...
    def simulate_loss(self) -> None: ...
    def restore(self, source: Path) -> None: ...
    def verify_schema(self) -> tuple[bool, str]: ...
    def verify_migration_head(self) -> tuple[bool, str]: ...
    def verify_rls_roles(self) -> tuple[bool, str]: ...
    def verify_audit_head(self) -> tuple[bool, str]: ...
    def verify_transport_state(self) -> tuple[bool, str]: ...
    def verify_locator_integrity(self) -> tuple[bool, str]: ...
    def reconcile(self) -> tuple[bool, str]: ...
    def verify_qdrant_references(self) -> tuple[bool, str]: ...
    def resource_count(self) -> tuple[bool, str]: ...
    def relation_count(self) -> tuple[bool, str]: ...


def run_restore_drill(
    adapter: DrillAdapter,
    work_dir: Path,
) -> RestoreDrillResult:
    """Execute the full drill chain; a failed step still records evidence
    for the remaining steps where the engine is usable."""
    started = time.monotonic()
    work = Path(work_dir)
    work.mkdir(parents=True, exist_ok=True)
    checks: list[DrillCheck] = []
    usable = True

    def step(name: str, fn: Any) -> None:
        nonlocal usable
        if not usable:
            checks.append(DrillCheck(name, False, "skipped:engine-unusable"))
            return
        try:
            passed, detail = fn()
        except Exception as exc:
            checks.append(DrillCheck(name, False, f"error:{exc}"[:200]))
            usable = False
            return
        checks.append(DrillCheck(name, bool(passed), str(detail)[:200]))
        if not passed:
            usable = False

    backup_path = work / "backup.snapshot"
    step("backup", lambda: (adapter.backup(backup_path), True, "backup taken")[1:])
    step("simulate_loss", lambda: (adapter.simulate_loss(), True, "loss simulated")[1:])
    step("restore", lambda: (adapter.restore(backup_path), True, "restored")[1:])
    step("schema_version", adapter.verify_schema)
    step("migration_head", adapter.verify_migration_head)
    step("rls_roles", adapter.verify_rls_roles)
    step("audit_sequence", adapter.verify_audit_head)
    step("transport_state", adapter.verify_transport_state)
    step("resource_count", adapter.resource_count)
    step("relation_count", adapter.relation_count)
    step("locator_integrity", adapter.verify_locator_integrity)
    step("reconcile_backlog", adapter.reconcile)
    step("qdrant_references", adapter.verify_qdrant_references)

    ready = all(c.passed for c in checks)
    return RestoreDrillResult(
        ready=ready,
        checks=checks,
        duration_seconds=time.monotonic() - started,
        engine_live=bool(getattr(adapter, "engine_live", False)),
    )


def run_live_restore_drill(
    adapter: DrillAdapter,
    work_dir: Path,
) -> RestoreDrillResult:
    """Production entry: run the drill and refuse test-scope evidence.

    A fake adapter may exercise the harness with ``engine_live=False``;
    production certification must never consume that as READY.  This entry
    raises :class:`DrillEngineUnavailable` instead of returning such a
    result, so callers cannot accidentally certify a non-live drill.
    """
    result = run_restore_drill(adapter, work_dir)
    if not bool(getattr(adapter, "engine_live", False)):
        raise DrillEngineUnavailable(
            "restore drill evidence is not from a live engine "
            "(engine_live=False)"
        )
    return result


# ---------------------------------------------------------------------------
# Controlled-scope adapter: real backup/loss/restore on a temp SQLite file.
# Proves the drill chain executes; production certification additionally
# requires engine_live=True from the PostgreSQL adapter.
# ---------------------------------------------------------------------------

class SQLiteFileDrillAdapter:
    """File-level drill adapter over a temporary SQLite database."""

    engine_live = False

    def __init__(self, db_path: Path, *, expected_schema_version: int = 1) -> None:
        self.db_path = Path(db_path)
        self.expected_schema_version = expected_schema_version
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version (v INTEGER NOT NULL)"
        )
        conn.execute("DELETE FROM schema_version")
        conn.execute("INSERT INTO schema_version VALUES (?)",
                     (expected_schema_version,))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS resource_metadata ("
            " module_id TEXT, resource_id TEXT, locator_id TEXT,"
            " version INTEGER, status TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS audit_event ("
            " event_id TEXT PRIMARY KEY, action TEXT, occurred_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS reconcile_state ("
            " module_id TEXT, resource_id TEXT, local_version INTEGER,"
            " local_updated_at TEXT, reconcile_status TEXT DEFAULT 'pending')"
        )
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db_path))

    def backup(self, destination: Path) -> None:
        src = self._connect()
        dst = sqlite3.connect(str(destination))
        src.backup(dst)  # online backup — consistent snapshot
        dst.close()
        src.close()

    def simulate_loss(self) -> None:
        self.db_path.unlink(missing_ok=False)

    def restore(self, source: Path) -> None:
        shutil.copyfile(source, self.db_path)

    def verify_schema(self) -> tuple[bool, str]:
        row = self._connect().execute(
            "SELECT v FROM schema_version"
        ).fetchone()
        ok = bool(row and int(row[0]) == self.expected_schema_version)
        return ok, f"schema_version={row[0] if row else 'missing'}"

    def verify_migration_head(self) -> tuple[bool, str]:
        return self.verify_schema()  # file-level: schema row is the head

    def verify_rls_roles(self) -> tuple[bool, str]:
        # SQLite has no RLS; the drill records the check as N/A-pass so the
        # chain shape is exercised.  The PG adapter implements the real
        # pg_class.relrowsecurity verification.
        return True, "n/a-sqlite"

    def verify_audit_head(self) -> tuple[bool, str]:
        row = self._connect().execute(
            "SELECT COUNT(*) FROM audit_event"
        ).fetchone()
        return True, f"audit_rows={row[0] if row else 0}"

    def verify_transport_state(self) -> tuple[bool, str]:
        return True, "n/a-sqlite"

    def verify_locator_integrity(self) -> tuple[bool, str]:
        row = self._connect().execute(
            "SELECT COUNT(*) FROM resource_metadata"
            " WHERE locator_id IS NULL OR locator_id = ''"
        ).fetchone()
        missing = int(row[0]) if row else 0
        return missing == 0, f"missing_locators={missing}"

    def reconcile(self) -> tuple[bool, str]:
        conn = self._connect()
        conn.execute(
            "UPDATE reconcile_state SET reconcile_status='in-sync'"
            " WHERE reconcile_status='pending'"
        )
        conn.commit()
        row = conn.execute(
            "SELECT COUNT(*) FROM reconcile_state"
            " WHERE reconcile_status='pending'"
        ).fetchone()
        pending = int(row[0]) if row else 0
        conn.close()
        return pending == 0, f"pending={pending}"

    def verify_qdrant_references(self) -> tuple[bool, str]:
        return True, "n/a-sqlite"

    def resource_count(self) -> tuple[bool, str]:
        row = self._connect().execute(
            "SELECT COUNT(*) FROM resource_metadata"
        ).fetchone()
        return True, f"resources={row[0] if row else 0}"

    def relation_count(self) -> tuple[bool, str]:
        return True, "n/a-sqlite"


__all__ = [
    "DrillAdapter",
    "DrillCheck",
    "DrillEngineUnavailable",
    "RestoreDrillResult",
    "SQLiteFileDrillAdapter",
    "run_live_restore_drill",
    "run_restore_drill",
]
