from __future__ import annotations

from dataclasses import dataclass

import psycopg

from .config import DatabaseSettings


@dataclass(frozen=True)
class PostgreSQLDetection:
    running: bool
    server_version: str | None = None
    error: str | None = None


def detect_postgresql(settings: DatabaseSettings) -> PostgreSQLDetection:
    """Detect an existing service. This function never installs or starts software."""
    try:
        with psycopg.connect(settings.admin_dsn, connect_timeout=3) as connection:
            row = connection.execute("SELECT current_setting('server_version')").fetchone()
        return PostgreSQLDetection(True, str(row[0]))
    except (psycopg.Error, OSError) as exc:
        return PostgreSQLDetection(False, error=str(exc)[:300])


__all__ = ["PostgreSQLDetection", "detect_postgresql"]
