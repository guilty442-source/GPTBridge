from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

import psycopg

from .config import DatabaseSettings
from .connection import database_dsn


@dataclass(frozen=True)
class DatabaseHealth:
    available: bool
    database: str
    latency_ms: float
    migration_count: int = 0
    error: str | None = None
    fault_code: str = "POSTGRESQL_READY"
    component: str = "postgresql"
    message: str = "ready"


class DatabaseHealthCheck:
    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings

    def run(self) -> DatabaseHealth:
        started = monotonic()
        try:
            with psycopg.connect(database_dsn(self.settings.admin_dsn, self.settings.database), connect_timeout=3) as connection:
                connection.execute("SELECT 1").fetchone()
                row = connection.execute("SELECT count(*) FROM gptbridge_migration.history").fetchone()
            return DatabaseHealth(
                True,
                self.settings.database,
                (monotonic() - started) * 1000,
                int(row[0]),
                fault_code="POSTGRESQL_READY",
                component="postgresql",
                message="ready",
            )
        except (psycopg.Error, OSError) as exc:
            detail = str(exc)[:300]
            return DatabaseHealth(
                False,
                self.settings.database,
                (monotonic() - started) * 1000,
                error=detail,
                fault_code="POSTGRESQL_UNAVAILABLE",
                component="postgresql",
                message=detail,
            )


__all__ = ["DatabaseHealth", "DatabaseHealthCheck"]
