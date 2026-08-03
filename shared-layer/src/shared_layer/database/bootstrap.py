from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import psycopg
from psycopg import sql

from .config import DatabaseSettings
from .connection import database_dsn
from .detection import detect_postgresql
from .migrations import MigrationRunner


@dataclass(frozen=True)
class BootstrapReport:
    database_created: bool
    roles_created: tuple[str, ...]
    migrations_applied: tuple[str, ...]


class DatabaseBootstrap:
    """Idempotent bootstrap; no psql, GUI, Docker, or manual SQL step."""

    def __init__(self, settings: DatabaseSettings, migrations: Path | str, base_schema: Path | str | None = None) -> None:
        self.settings = settings
        self.migrations = MigrationRunner(migrations)
        self.base_schema = None if base_schema is None else Path(base_schema)

    def run(self) -> BootstrapReport:
        if not detect_postgresql(self.settings).running:
            raise RuntimeError("POSTGRESQL_SERVICE_NOT_READY")
        created_roles: list[str] = []
        database_created = False
        with psycopg.connect(self.settings.admin_dsn, autocommit=True) as admin:
            for role in (self.settings.owner_role, self.settings.runtime_role, self.settings.reader_role):
                exists = admin.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,)).fetchone()
                if not exists:
                    admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
                    created_roles.append(role)
            exists = admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (self.settings.database,)).fetchone()
            if not exists:
                admin.execute(sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(self.settings.database), sql.Identifier(self.settings.owner_role)
                ))
                database_created = True
        with psycopg.connect(database_dsn(self.settings.admin_dsn, self.settings.database)) as connection:
            if self.base_schema is not None:
                connection.execute(self.base_schema.read_text(encoding="utf-8"))
            result = self.migrations.run(connection)
        return BootstrapReport(database_created, tuple(created_roles), result.applied)


__all__ = ["BootstrapReport", "DatabaseBootstrap"]
