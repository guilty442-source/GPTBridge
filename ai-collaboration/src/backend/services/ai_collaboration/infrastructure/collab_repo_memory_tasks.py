from __future__ import annotations

import json
import uuid
from typing import Any

from .collab_repo_constants import utc_now


class CollabRepoMemoryTasksMixin:
    """Memory-item, task, and JSON-helper methods for AiCollaborationRepository."""

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

    @staticmethod
    def _json_object(value: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
