"""database — codex-native local storage settings and health.

Module-private state and bounded degraded fallback live in PostgreSQL
schemas (A610/A621 — PostgreSQL is the canonical central structured-data
engine and the sole structured-data authority; the SQLite fleet is
retired).  These helpers probe a module-owned schema through
``pg_adapter`` and report reachability/latency/table count.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any


_SQL_IDENTIFIER = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_LOCAL_PATH = re.compile(r"^local:[a-z0-9][a-z0-9_\-/]{0,511}$")


@dataclass(frozen=True)
class LocalDatabaseSettings:
    """Local store settings. ``database`` names the module's PG schema."""

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
    fault_code: str = "LOCAL_POSTGRES_READY"
    component: str = "local-postgresql"
    message: str = "ready"


class LocalDatabaseHealthCheck:
    def __init__(self, settings: LocalDatabaseSettings | None = None) -> None:
        self.settings = settings or LocalDatabaseSettings()

    def run(self) -> LocalDatabaseHealth:
        from .pg_adapter import connect as pg_connect

        started = monotonic()
        try:
            with pg_connect(self.settings.database) as connection:
                connection.execute("SELECT 1").fetchone()
                row = connection.execute(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema = current_schema()"
                ).fetchone()
                table_count = int(row[0]) if row else 0
            return LocalDatabaseHealth(
                True,
                self.settings.database,
                (monotonic() - started) * 1000,
                table_count,
                fault_code="LOCAL_POSTGRES_READY",
                component="local-postgresql",
                message="ready",
            )
        except Exception as exc:
            detail = str(exc)[:300]
            return LocalDatabaseHealth(
                False,
                self.settings.database,
                (monotonic() - started) * 1000,
                error=detail,
                fault_code="LOCAL_POSTGRES_UNAVAILABLE",
                component="local-postgresql",
                message=detail,
            )


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
