from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict, make_conninfo

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
    def _dsn_without_password(dsn: str) -> str:
        return make_conninfo(
            **{k: v for k, v in conninfo_to_dict(dsn).items() if k != "password"}
        )

    @staticmethod
    def _pgpass(dsn: str) -> str:
        password = conninfo_to_dict(dsn).get("password")
        if not password:
            return ""
        temp_root = Path(
            os.environ.get("GPTBRIDGE_TOOL_TEMP_ROOT")
            or os.environ.get("GPTBRIDGE_GLOBAL_TEMP_ROOT")
            or r"E:\AI\caches\gptbridge\pgpass"
        ).resolve()
        temp_root.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".pgpass",
            delete=False,
            encoding="utf-8",
            dir=temp_root,
        )
        try:
            escaped_password = str(password).replace("\\", "\\\\").replace(":", "\\:")
            handle.write(f"*:*:*:*:{escaped_password}\n")
        finally:
            handle.close()
        os.chmod(handle.name, 0o600)
        return handle.name

    @staticmethod
    def _remove_pgpass(path: str) -> None:
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def _run_tool(
        self,
        command: list[str],
        dsn: str,
    ) -> None:
        pgpass_path = self._pgpass(dsn)
        environment = dict(os.environ)
        if pgpass_path:
            environment["PGPASSFILE"] = pgpass_path
        try:
            subprocess.run(
                command,
                check=True,
                shell=False,
                env=environment,
                creationflags=(
                    subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                ),
            )
        finally:
            self._remove_pgpass(pgpass_path)

    def backup(self, destination: Path | str) -> BackupResult:
        target = Path(destination).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        dsn = database_dsn(self.settings.admin_dsn, self.settings.database)
        self._run_tool(
            [
                self._tool("pg_dump"),
                "--dbname",
                self._dsn_without_password(dsn),
                "--format=custom",
                "--file",
                str(target),
            ],
            dsn,
        )
        return BackupResult(target, target.stat().st_size)

    def restore(self, source: Path | str) -> None:
        backup = Path(source).resolve()
        if not backup.is_file():
            raise FileNotFoundError(backup)
        dsn = database_dsn(self.settings.admin_dsn, self.settings.database)
        self._run_tool(
            [
                self._tool("pg_restore"),
                "--dbname",
                self._dsn_without_password(dsn),
                "--clean",
                "--if-exists",
                "--single-transaction",
                str(backup),
            ],
            dsn,
        )


__all__ = ["BackupOrchestrator", "BackupResult"]
