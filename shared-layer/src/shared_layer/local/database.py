"""database — codex-native local storage settings and health.

Stdlib-only local sqlite helpers for module-private state, transport and
bounded degraded fallback (A219/E21 + A37/E23 + A44/E30).  PostgreSQL is the
canonical central structured-data engine; these sqlite records are
module-owned, never authoritative, and must be reconciled with the central
store.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any


_SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_LOCAL_PATH = re.compile(r"^local:[a-z0-9][a-z0-9_\-/]{0,511}$")


@dataclass(frozen=True)
class LocalDatabaseSettings:
    """Local sqlite settings. ``database`` holds the relative local root key."""

    admin_dsn: str = "local:gptbridge"
    database: str = "gptbridge"
    owner_role: str = "gptbridge_owner"
    runtime_role: str = "gptbridge_runtime"
    reader_role: str = "gptbridge_xingcheng_reader"

    def __post_init__(self) -> None:
        if not _LOCAL_PATH.fullmatch(self.admin_dsn):
            raise ValueError("GPTBRIDGE_LOCAL_DB_REQUIRED")
        for field in ("database", "owner_role", "runtime_role", "reader_role"):
            if not _SQL_IDENTIFIER.fullmatch(getattr(self, field)):
                raise ValueError(f"INVALID_LOCAL_IDENTIFIER:{field}")

    @classmethod
    def from_environment(cls) -> "LocalDatabaseSettings":
        return cls(
            admin_dsn=os_environ_get("GPTBRIDGE_LOCAL_DB", "local:gptbridge"),
            database=os_environ_get("GPTBRIDGE_LOCAL_DATABASE", "gptbridge"),
            owner_role=os_environ_get("GPTBRIDGE_LOCAL_OWNER_ROLE", "gptbridge_owner"),
            runtime_role=os_environ_get("GPTBRIDGE_LOCAL_RUNTIME_ROLE", "gptbridge_runtime"),
            reader_role=os_environ_get("GPTBRIDGE_LOCAL_READER_ROLE", "gptbridge_xingcheng_reader"),
        )


def os_environ_get(key: str, default: str) -> str:
    import os

    return os.environ.get(key, default)


@dataclass(frozen=True)
class LocalDatabaseHealth:
    available: bool
    database: str
    latency_ms: float
    migration_count: int = 0
    error: str | None = None
    fault_code: str = "LOCAL_SQLITE_READY"
    component: str = "local-sqlite"
    message: str = "ready"


class LocalDatabaseHealthCheck:
    def __init__(self, settings: LocalDatabaseSettings | None = None) -> None:
        self.settings = settings or LocalDatabaseSettings()
        # AA5: table-count cache keyed by db file (mtime, size) — the schema
        # only changes on migration, so re-counting sqlite_master on every
        # probe is wasted work.
        self._schema_cache: tuple[tuple[int, int], int] | None = None

    def run(self) -> LocalDatabaseHealth:
        started = monotonic()
        db_path = _local_path(self.settings)
        try:
            stat_key = _db_stat_key(db_path)
            with sqlite3.connect(_test_connection_target(db_path)) as connection:
                connection.execute("SELECT 1").fetchone()
                table_count = self._table_count(connection, stat_key)
            return LocalDatabaseHealth(
                True,
                self.settings.database,
                (monotonic() - started) * 1000,
                table_count,
                fault_code="LOCAL_SQLITE_READY",
                component="local-sqlite",
                message="ready",
            )
        except (sqlite3.Error, OSError) as exc:
            detail = str(exc)[:300]
            return LocalDatabaseHealth(
                False,
                self.settings.database,
                (monotonic() - started) * 1000,
                error=detail,
                fault_code="LOCAL_SQLITE_UNAVAILABLE",
                component="local-sqlite",
                message=detail,
            )


    def _table_count(
        self, connection: sqlite3.Connection, stat_key: tuple[int, int] | None
    ) -> int:
        if stat_key is not None and self._schema_cache is not None:
            cached_key, cached_count = self._schema_cache
            if cached_key == stat_key:
                return cached_count
        row = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()
        count = int(row[0]) if row else 0
        if stat_key is not None:
            self._schema_cache = (stat_key, count)
        return count


def _db_stat_key(db_path: Path) -> tuple[int, int] | None:
    try:
        stat = db_path.stat()
    except OSError:
        return None
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _test_connection_target(db_path: Path) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


def _default_project_root() -> Path:
    """AA11/X13: derive the repo root from this module's location instead of
    a hardcoded absolute path (portable checkouts / worktrees)."""
    # shared-layer/src/shared_layer/local/database.py → repo root is 5 up.
    return Path(__file__).resolve().parents[4]


def _local_path(settings: LocalDatabaseSettings) -> Path:
    import os

    raw = str(os.environ.get("GPTBRIDGE_GOVERNANCE_PROJECT_ROOT") or "").strip()
    root = Path(raw).resolve() if raw else _default_project_root()
    relative = settings.admin_dsn[len("local:"):].strip("/").replace("\\", "/")
    if not relative or ".." in relative.split("/"):
        relative = "shared-layer/runtime"
    return (root / relative).resolve()


DatabaseSettings = LocalDatabaseSettings
DatabaseHealth = LocalDatabaseHealth
DatabaseHealthCheck = LocalDatabaseHealthCheck

__all__ = [
    "DatabaseSettings",
    "DatabaseHealth",
    "DatabaseHealthCheck",
    "LocalDatabaseSettings",
    "LocalDatabaseHealth",
    "LocalDatabaseHealthCheck",
]