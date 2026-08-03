from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


class RepairRunStore:
    """Persist repair evidence in one isolated database per target tool."""

    def __init__(self, database_root: Path) -> None:
        self.database_root = database_root.resolve()

    def _connect(self, target_id: str) -> tuple[sqlite3.Connection, Path]:
        owner_root = self.database_root / target_id
        owner_root.mkdir(parents=True, exist_ok=True)
        path = owner_root / "automatic-repair.sqlite3"
        connection = sqlite3.connect(path, timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS repair_runs ("
            "run_id TEXT PRIMARY KEY, target_tool_id TEXT NOT NULL, "
            "started_at TEXT NOT NULL, completed_at TEXT NOT NULL, "
            "failure_code TEXT NOT NULL, ok INTEGER NOT NULL, "
            "detail_json TEXT NOT NULL)"
        )
        return connection, path

    def record(self, target_id: str, result: dict[str, Any]) -> Path:
        connection, path = self._connect(target_id)
        try:
            connection.execute(
                "INSERT INTO repair_runs "
                "(run_id, target_tool_id, started_at, completed_at, failure_code, ok, detail_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    result["run_id"],
                    target_id,
                    result["started_at"],
                    result["completed_at"],
                    result["failure_code"],
                    int(bool(result["ok"])),
                    json.dumps(result, ensure_ascii=False),
                ),
            )
            connection.commit()
        finally:
            connection.close()
        return path


__all__ = ["RepairRunStore"]
