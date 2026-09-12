from __future__ import annotations

import json
import uuid
from typing import Any

from .collab_repo_constants import utc_now


class CollabRepoMessagesMixin:
    """Group-message and agent-response methods for AiCollaborationRepository."""

    def create_group_message(
        self,
        content: str,
        selected_agents: list[str],
        business_scope: str = "general",
    ) -> dict[str, Any]:
        message_id = uuid.uuid4().hex[:16]
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO ai_nexus_group_messages
                (message_id, role, content, selected_agents_json, business_scope, created_at)
                VALUES (?, 'user', ?, ?, ?, ?)
                """,
                (
                    message_id,
                    content,
                    json.dumps(selected_agents, ensure_ascii=False),
                    business_scope,
                    now,
                ),
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

    def update_response(
        self,
        message_id: str,
        agent_id: str,
        status: str,
        content: str = "",
        error: str = "",
        *,
        error_code: str = "",
        execution_provider: str = "",
        transport: str = "",
        fallback: dict[str, Any] | None = None,
        memory_candidates: list[dict[str, Any]] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ai_nexus_agent_responses
                SET status = ?, content = ?, error = ?, error_code = ?,
                    execution_provider = ?, transport = ?, fallback_json = ?,
                    memory_candidates_json = ?, updated_at = ?
                WHERE message_id = ? AND agent_id = ?
                """,
                (
                    status,
                    content,
                    error,
                    error_code,
                    execution_provider,
                    transport,
                    json.dumps(fallback or {}, ensure_ascii=False),
                    json.dumps(memory_candidates or [], ensure_ascii=False),
                    utc_now(),
                    message_id,
                    agent_id,
                ),
            )

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT message_id, role, content, selected_agents_json, business_scope, created_at FROM ai_nexus_group_messages WHERE message_id = ?",
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
                SELECT message_id, role, content, selected_agents_json, business_scope, created_at FROM ai_nexus_group_messages
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
                    SELECT response_id, message_id, agent_id, status, content, error, error_code, execution_provider, transport, fallback_json, memory_candidates_json, created_at, updated_at FROM ai_nexus_agent_responses
                    WHERE message_id IN ({placeholders})
                    ORDER BY created_at ASC
                    """,
                    message_ids,
                ).fetchall()
                for response in response_rows:
                    response_item = dict(response)
                    response_item["fallback"] = self._json_object(
                        response_item.pop("fallback_json", "{}")
                    )
                    response_item["memory_candidates"] = self._json_list(
                        response_item.pop("memory_candidates_json", "[]")
                    )
                    responses[str(response["message_id"])].append(response_item)
        output = []
        for row in reversed(rows):
            item = dict(row)
            item["selected_agents"] = self._json_list(item.pop("selected_agents_json", "[]"))
            item["responses"] = responses.get(str(row["message_id"]), [])
            output.append(item)
        return output
