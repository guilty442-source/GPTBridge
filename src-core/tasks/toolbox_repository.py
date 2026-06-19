from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable


class ToolboxRepository:
    """SQLite-backed repository for platform tool metadata."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.db_path = self.project_root / "runtime" / "state" / "gptbridge.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS toolbox_tools (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL DEFAULT '1.0.0',
                    status TEXT NOT NULL DEFAULT 'stopped',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    entry TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    manifest_path TEXT NOT NULL DEFAULT '',
                    code_path TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_toolbox_tools_enabled
                ON toolbox_tools(enabled)
                """
            )

    @staticmethod
    def _normalize_tool(tool: Dict[str, Any]) -> Dict[str, Any]:
        tool_id = str(tool.get("id", "")).strip()
        name = str(tool.get("name", tool_id)).strip() or tool_id
        status = str(tool.get("status", "stopped")).strip() or "stopped"
        return {
            "id": tool_id,
            "name": name,
            "version": str(tool.get("version", "1.0.0")).strip() or "1.0.0",
            "status": status,
            "enabled": 1 if tool.get("enabled", True) is not False else 0,
            "entry": str(tool.get("entry", "")).strip(),
            "description": str(tool.get("description", "")).strip(),
            "manifest_path": str(tool.get("manifest_path", "")).strip(),
            "code_path": str(tool.get("code_path", "")).strip(),
        }

    def upsert_tool(self, tool: Dict[str, Any]) -> None:
        normalized = self._normalize_tool(tool)
        if not normalized["id"]:
            return

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO toolbox_tools (
                    id, name, version, status, enabled, entry,
                    description, manifest_path, code_path, updated_at
                )
                VALUES (
                    :id, :name, :version, :status, :enabled, :entry,
                    :description, :manifest_path, :code_path, CURRENT_TIMESTAMP
                )
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    version = excluded.version,
                    status = excluded.status,
                    enabled = excluded.enabled,
                    entry = excluded.entry,
                    description = excluded.description,
                    manifest_path = excluded.manifest_path,
                    code_path = excluded.code_path,
                    updated_at = CURRENT_TIMESTAMP
                """,
                normalized,
            )

    def replace_tools(self, tools: Iterable[Dict[str, Any]]) -> None:
        normalized_tools = [
            self._normalize_tool(tool)
            for tool in tools
            if str(tool.get("id", "")).strip()
        ]
        active_ids = {tool["id"] for tool in normalized_tools}

        with self._connect() as connection:
            if active_ids:
                placeholders = ",".join("?" for _ in active_ids)
                connection.execute(
                    f"DELETE FROM toolbox_tools WHERE id NOT IN ({placeholders})",
                    tuple(sorted(active_ids)),
                )
            else:
                connection.execute("DELETE FROM toolbox_tools")

            for tool in normalized_tools:
                connection.execute(
                    """
                    INSERT INTO toolbox_tools (
                        id, name, version, status, enabled, entry,
                        description, manifest_path, code_path, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                    ON CONFLICT(id) DO UPDATE SET
                        name = excluded.name,
                        version = excluded.version,
                        status = excluded.status,
                        enabled = excluded.enabled,
                        entry = excluded.entry,
                        description = excluded.description,
                        manifest_path = excluded.manifest_path,
                        code_path = excluded.code_path,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (
                        tool["id"],
                        tool["name"],
                        tool["version"],
                        tool["status"],
                        tool["enabled"],
                        tool["entry"],
                        tool["description"],
                        tool["manifest_path"],
                        tool["code_path"],
                    ),
                )

    def list_tools(self) -> list[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, version, status, enabled, entry,
                       description, manifest_path, code_path, updated_at
                FROM toolbox_tools
                ORDER BY name COLLATE NOCASE, id COLLATE NOCASE
                """
            ).fetchall()

        return [
            {
                "id": row["id"],
                "name": row["name"],
                "version": row["version"],
                "status": row["status"],
                "enabled": bool(row["enabled"]),
                "entry": row["entry"],
                "description": row["description"],
                "manifest_path": row["manifest_path"],
                "code_path": row["code_path"],
                "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def update_status(self, tool_id: str, status: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE toolbox_tools
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (status, tool_id),
            )
            return cursor.rowcount > 0

    def delete_tool(self, tool_id: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM toolbox_tools WHERE id = ?", (tool_id,))
