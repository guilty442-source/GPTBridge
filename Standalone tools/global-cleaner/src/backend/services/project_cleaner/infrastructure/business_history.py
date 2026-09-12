from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class BusinessHistoryStore:
    """Project Cleaner-owned business history database."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.resolve()

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path)
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS cleanup_history (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                operation_id TEXT NOT NULL UNIQUE,
                timestamp TEXT NOT NULL,
                action TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        return connection

    def append(self, action: str, **payload: Any) -> None:
        operation_id = uuid.uuid4().hex
        timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO cleanup_history (
                        operation_id, timestamp, action, payload_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        operation_id,
                        timestamp,
                        action,
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )
        finally:
            connection.close()

    def read(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not self.database_path.exists() or limit <= 0:
            return []
        connection = self._connect()
        try:
            with connection:
                rows = connection.execute(
                    """
                    SELECT operation_id, timestamp, action, payload_json
                    FROM cleanup_history
                    ORDER BY sequence DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        finally:
            connection.close()
        records: list[dict[str, Any]] = []
        for operation_id, timestamp, action, payload_json in reversed(rows):
            try:
                payload = json.loads(payload_json)
            except json.JSONDecodeError:
                payload = {}
            records.append(
                {
                    "operation_id": operation_id,
                    "timestamp": timestamp,
                    "action": action,
                    **(payload if isinstance(payload, dict) else {}),
                }
            )
        return records
