from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


DEFAULT_AGENTS: tuple[dict[str, str], ...] = (
    {
        "agent_id": "chatgpt",
        "name": "ChatGPT",
        "provider": "chatgpt",
        "home_url": "https://chatgpt.com/",
    },
    {
        "agent_id": "claude",
        "name": "Claude",
        "provider": "claude",
        "home_url": "https://claude.ai/",
    },
    {
        "agent_id": "gemini",
        "name": "Gemini",
        "provider": "gemini",
        "home_url": "https://gemini.google.com/",
    },
    {
        "agent_id": "grok",
        "name": "Grok",
        "provider": "grok",
        "home_url": "https://grok.com/",
    },
    {
        "agent_id": "deepseek",
        "name": "DeepSeek",
        "provider": "deepseek",
        "home_url": "https://chat.deepseek.com/",
    },
)


class AiCollaborationRepository:
    def __init__(self, project_root: Path) -> None:
        self.db_path = project_root / "runtime" / "state" / "ai_collaboration.sqlite3"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()
        self._ensure_default_agents()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS ai_nexus_agents (
                    agent_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    home_url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    selected INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'idle',
                    last_error TEXT NOT NULL DEFAULT '',
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_nexus_group_messages (
                    message_id TEXT PRIMARY KEY,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    selected_agents_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_ai_nexus_group_messages_created
                ON ai_nexus_group_messages(created_at DESC);

                CREATE TABLE IF NOT EXISTS ai_nexus_agent_responses (
                    response_id TEXT PRIMARY KEY,
                    message_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    content TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY(message_id) REFERENCES ai_nexus_group_messages(message_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_ai_nexus_agent_responses_message
                ON ai_nexus_agent_responses(message_id, agent_id);

                CREATE TABLE IF NOT EXISTS ai_nexus_workspaces (
                    workspace_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    root_path TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_nexus_tasks (
                    task_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    source_message_id TEXT NOT NULL DEFAULT '',
                    participant_agents_json TEXT NOT NULL DEFAULT '[]',
                    conclusion TEXT NOT NULL DEFAULT '',
                    files_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_nexus_memory_items (
                    memory_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    content TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_nexus_knowledge_files (
                    file_id TEXT PRIMARY KEY,
                    file_path TEXT NOT NULL,
                    file_type TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL DEFAULT '',
                    content_text TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS ai_nexus_tools (
                    tool_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    command TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )

    def _ensure_default_agents(self) -> None:
        now = utc_now()
        with self._connect() as connection:
            for agent in DEFAULT_AGENTS:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO ai_nexus_agents
                    (agent_id, name, provider, home_url, enabled, selected, status, updated_at)
                    VALUES (?, ?, ?, ?, 1, 1, 'idle', ?)
                    """,
                    (
                        agent["agent_id"],
                        agent["name"],
                        agent["provider"],
                        agent["home_url"],
                        now,
                    ),
                )

    def list_agents(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_nexus_agents ORDER BY rowid"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_agents(self, agent_ids: list[str]) -> list[dict[str, Any]]:
        if not agent_ids:
            return []
        placeholders = ",".join("?" for _ in agent_ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM ai_nexus_agents WHERE agent_id IN ({placeholders}) ORDER BY rowid",
                agent_ids,
            ).fetchall()
        found = {str(row["agent_id"]): dict(row) for row in rows}
        return [found[agent_id] for agent_id in agent_ids if agent_id in found]

    def save_agent_selection(self, agent_ids: list[str]) -> None:
        selected = set(agent_ids)
        now = utc_now()
        with self._connect() as connection:
            connection.execute("UPDATE ai_nexus_agents SET selected = 0, updated_at = ?", (now,))
            for agent_id in selected:
                connection.execute(
                    "UPDATE ai_nexus_agents SET selected = 1, updated_at = ? WHERE agent_id = ?",
                    (now, agent_id),
                )

    def update_agent_status(self, agent_id: str, status: str, error: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ai_nexus_agents
                SET status = ?, last_error = ?, updated_at = ?
                WHERE agent_id = ?
                """,
                (status, error, utc_now(), agent_id),
            )

    def create_group_message(self, content: str, selected_agents: list[str]) -> dict[str, Any]:
        message_id = uuid.uuid4().hex[:16]
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_nexus_group_messages
                (message_id, role, content, selected_agents_json, created_at)
                VALUES (?, 'user', ?, ?, ?)
                """,
                (message_id, content, json.dumps(selected_agents, ensure_ascii=False), now),
            )
            for agent_id in selected_agents:
                connection.execute(
                    """
                    INSERT INTO ai_nexus_agent_responses
                    (response_id, message_id, agent_id, status, created_at, updated_at)
                    VALUES (?, ?, ?, 'pending', ?, ?)
                    """,
                    (uuid.uuid4().hex[:16], message_id, agent_id, now, now),
                )
        return self.get_message(message_id) or {}

    def update_response(self, message_id: str, agent_id: str, status: str, content: str = "", error: str = "") -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ai_nexus_agent_responses
                SET status = ?, content = ?, error = ?, updated_at = ?
                WHERE message_id = ? AND agent_id = ?
                """,
                (status, content, error, utc_now(), message_id, agent_id),
            )

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM ai_nexus_group_messages WHERE message_id = ?",
                (message_id,),
            ).fetchone()
        if row is None:
            return None
        messages = self.list_messages(limit=1, message_id=message_id)
        return messages[0] if messages else None

    def list_messages(self, limit: int = 50, message_id: str = "") -> list[dict[str, Any]]:
        params: list[Any] = []
        where = ""
        if message_id:
            where = "WHERE message_id = ?"
            params.append(message_id)
        params.append(max(1, min(200, limit)))
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM ai_nexus_group_messages
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
            message_ids = [str(row["message_id"]) for row in rows]
            responses: dict[str, list[dict[str, Any]]] = {key: [] for key in message_ids}
            if message_ids:
                placeholders = ",".join("?" for _ in message_ids)
                response_rows = connection.execute(
                    f"""
                    SELECT * FROM ai_nexus_agent_responses
                    WHERE message_id IN ({placeholders})
                    ORDER BY created_at ASC
                    """,
                    message_ids,
                ).fetchall()
                for response in response_rows:
                    responses[str(response["message_id"])].append(dict(response))
        output = []
        for row in reversed(rows):
            item = dict(row)
            item["selected_agents"] = self._json_list(item.pop("selected_agents_json", "[]"))
            item["responses"] = responses.get(str(row["message_id"]), [])
            output.append(item)
        return output

    def list_memory_items(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_nexus_memory_items ORDER BY updated_at DESC LIMIT 100"
            ).fetchall()
        return [dict(row) for row in rows]

    def add_memory_item(self, kind: str, title: str, content: str) -> dict[str, Any]:
        now = utc_now()
        memory_id = uuid.uuid4().hex[:16]
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_nexus_memory_items
                (memory_id, kind, title, content, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (memory_id, kind, title, content, now, now),
            )
        return {
            "memory_id": memory_id,
            "kind": kind,
            "title": title,
            "content": content,
            "created_at": now,
            "updated_at": now,
        }

    def list_tasks(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ai_nexus_tasks ORDER BY updated_at DESC LIMIT 100"
            ).fetchall()
        output = []
        for row in rows:
            item = dict(row)
            item["participant_agents"] = self._json_list(item.pop("participant_agents_json", "[]"))
            item["files"] = self._json_list(item.pop("files_json", "[]"))
            output.append(item)
        return output

    def create_task(self, title: str, source_message_id: str = "", participant_agents: list[str] | None = None) -> dict[str, Any]:
        now = utc_now()
        task_id = uuid.uuid4().hex[:16]
        agents = participant_agents or []
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_nexus_tasks
                (task_id, title, status, source_message_id, participant_agents_json, files_json, created_at, updated_at)
                VALUES (?, ?, 'pending', ?, ?, '[]', ?, ?)
                """,
                (task_id, title, source_message_id, json.dumps(agents, ensure_ascii=False), now, now),
            )
        return {
            "task_id": task_id,
            "title": title,
            "status": "pending",
            "source_message_id": source_message_id,
            "participant_agents": agents,
            "files": [],
            "conclusion": "",
            "created_at": now,
            "updated_at": now,
        }

    @staticmethod
    def _json_list(value: str) -> list[Any]:
        try:
            parsed = json.loads(value)
        except Exception:
            return []
        return parsed if isinstance(parsed, list) else []
