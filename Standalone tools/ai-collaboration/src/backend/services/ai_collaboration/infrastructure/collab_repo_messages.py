from __future__ import annotations

import json
import uuid
from typing import Any

from .collab_repo_constants import utc_now

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})

_RESPONSE_COLUMNS = (
    "response_id, message_id, agent_id, status, content, error, error_code, "
    "execution_provider, transport, fallback_json, memory_candidates_json, "
    "request_id, runtime_generation, response_state, result_reference, "
    "created_at, completed_at, updated_at"
)

_MESSAGE_COLUMNS = (
    "message_id, role, content, selected_agents_json, business_scope, "
    "request_id, runtime_generation, created_at"
)


class CollabRepoMessagesMixin:
    """Group-message and agent-response methods for AiCollaborationRepository."""

    def create_group_message(
        self,
        content: str,
        selected_agents: list[str],
        business_scope: str = "general",
        *,
        request_id: str = "",
        runtime_generation: str = "",
    ) -> dict[str, Any]:
        message_id = uuid.uuid4().hex[:16]
        now = utc_now()
        with self._connect() as connection:
            connection.execute(
                f"""
                INSERT INTO ai_nexus_group_messages
                (message_id, role, content, selected_agents_json, business_scope,
                 request_id, runtime_generation, created_at)
                VALUES (?, 'user', ?, ?, ?, ?, ?, ?)
                """,  # sql-ok: fixed column list
                (
                    message_id,
                    content,
                    json.dumps(selected_agents, ensure_ascii=False),
                    business_scope,
                    str(request_id or ""),
                    str(runtime_generation or ""),
                    now,
                ),
            )
            connection.executemany(
                """
                INSERT INTO ai_nexus_agent_responses
                (response_id, message_id, agent_id, status,
                 request_id, runtime_generation, created_at, updated_at)
                VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                [
                    (
                        uuid.uuid4().hex[:16],
                        message_id,
                        agent_id,
                        str(request_id or ""),
                        str(runtime_generation or ""),
                        now,
                        now,
                    )
                    for agent_id in selected_agents
                ],
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
        response_state: str = "",
        result_reference: str = "",
    ) -> None:
        now = utc_now()
        completed_at = now if status in _TERMINAL_STATUSES else ""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ai_nexus_agent_responses
                SET status = ?, content = ?, error = ?, error_code = ?,
                    execution_provider = ?, transport = ?, fallback_json = ?,
                    memory_candidates_json = ?,
                    response_state = CASE WHEN ? != '' THEN ? ELSE response_state END,
                    result_reference = CASE WHEN ? != '' THEN ? ELSE result_reference END,
                    completed_at = CASE WHEN ? != '' THEN ? ELSE completed_at END,
                    updated_at = ?
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
                    response_state,
                    response_state,
                    result_reference,
                    result_reference,
                    completed_at,
                    completed_at,
                    now,
                    message_id,
                    agent_id,
                ),
            )

    def cancel_pending_responses(self, message_id: str) -> int:
        """Mark every non-terminal response of a message as cancelled."""
        now = utc_now()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE ai_nexus_agent_responses
                SET status = 'cancelled', error = 'REQUEST_CANCELLED',
                    error_code = 'REQUEST_CANCELLED',
                    completed_at = ?, updated_at = ?
                WHERE message_id = ?
                  AND status NOT IN ('completed', 'failed', 'cancelled')
                """,
                (now, now, message_id),
            )
            return int(cursor.rowcount or 0)

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(  # sql-ok: fixed column list
                f"SELECT {_MESSAGE_COLUMNS} FROM ai_nexus_group_messages WHERE message_id = ?",
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
            rows = connection.execute(  # sql-ok: code-built fragment, values parameterized
                f"""
                SELECT {_MESSAGE_COLUMNS} FROM ai_nexus_group_messages
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
                response_rows = connection.execute(  # sql-ok: generated ? placeholder list
                    f"""
                    SELECT {_RESPONSE_COLUMNS} FROM ai_nexus_agent_responses
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
