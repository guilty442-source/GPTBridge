from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

from .config import DatabaseSettings
from .connection import database_dsn


@dataclass(frozen=True)
class BackupResult:
    path: Path
    size: int


class BackupOrchestrator:
    """Python orchestration around PostgreSQL's native, consistent backup format."""

    def __init__(self, settings: DatabaseSettings) -> None:
        self.settings = settings

    @staticmethod
    def _tool(name: str) -> str:
        executable = shutil.which(name)
        if not executable:
            raise RuntimeError(f"POSTGRES_TOOL_NOT_FOUND:{name}")
        return executable

    @staticmethod
    def _environment(dsn: str) -> dict[str, str]:
        environment = dict(os.environ)
        password = conninfo_to_dict(dsn).get("password")
        if password:
            environment["PGPASSWORD"] = password
        return environment

    def backup(self, destination: Path | str) -> BackupResult:
        target = Path(destination).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        dsn = database_dsn(self.settings.admin_dsn, self.settings.database)
        subprocess.run(
            [self._tool("pg_dump"), "--dbname", dsn, "--format=custom", "--file", str(target)],
            check=True, shell=False, env=self._environment(dsn),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        return BackupResult(target, target.stat().st_size)

    def restore(self, source: Path | str) -> None:
        backup = Path(source).resolve()
        if not backup.is_file():
            raise FileNotFoundError(backup)
        dsn = database_dsn(self.settings.admin_dsn, self.settings.database)
        subprocess.run(
            [self._tool("pg_restore"), "--dbname", dsn, "--clean", "--if-exists", "--single-transaction", str(backup)],
            check=True, shell=False, env=self._environment(dsn),
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )


__all__ = ["BackupOrchestrator", "BackupResult"]
