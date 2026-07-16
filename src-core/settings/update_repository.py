from __future__ import annotations

import sqlite3
from pathlib import Path


class UpdateRepository:
    """Main-process update state; never stores independent-tool business data."""

    def __init__(self, project_root: Path) -> None:
        self.database_path = (
            Path(project_root) / "runtime" / "state" / "main" / "updates.sqlite3"
        )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS update_snapshot (
                    relative_path TEXT PRIMARY KEY,
                    modified_ns INTEGER NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS repair_run (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool_id TEXT NOT NULL,
                    ok INTEGER NOT NULL,
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def load_snapshot(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT relative_path, modified_ns FROM update_snapshot"
            ).fetchall()
        return {str(path): int(modified_ns) for path, modified_ns in rows}

    def replace_snapshot(self, snapshot: dict[str, int]) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM update_snapshot")
            connection.executemany(
                "INSERT INTO update_snapshot(relative_path, modified_ns) VALUES (?, ?)",
                sorted(snapshot.items()),
            )

    def record_repair(self, tool_id: str, ok: bool, message: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO repair_run(tool_id, ok, message) VALUES (?, ?, ?)",
                (tool_id, 1 if ok else 0, message[:1000]),
            )
