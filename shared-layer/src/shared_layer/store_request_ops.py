"""Request-side operations mixin for PostgresSharedLayerStore (A185 split).

Submit / idempotency lookup / cancel / consume-response / progress —
every method borrows a pooled connection via ``_get_pool()``, binds the
short-term session identity and commits through the same transaction
boundary as before the split.
"""

from __future__ import annotations

from typing import Any

from .store_helpers import (
    _PRIORITY_VALUES,
    _normalize_priority_class,
    decode as _decode,
    encode_json as _json,
    normalize_id as _id,
)


class PostgresStoreRequestMixin:
    """Caller-side request operations for PostgresSharedLayerStore."""

    _channel_id: str

    def submit_request(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        payload: Any,
        *,
        priority_class: str = "interactive",
        deadline_at: Any = None,
        idempotency_key: str = "",
    ) -> None:
        actor = self._authorize(token, "request", target_tool_id)
        priority = _normalize_priority_class(priority_class)
        if idempotency_key:
            existing = self.find_request_by_idempotency(token, target_tool_id, idempotency_key)
            if existing is not None:
                return
        pool = self._get_pool()
        with pool.acquire() as connection:
            self._bind_identity(connection, actor, request_id)
            connection.execute(
                "INSERT INTO gptbridge_transport.tool_request "
                "(channel_id, request_id, requester_actor, target_tool_id, payload, status, "
                "priority_class, priority_value, deadline_at, idempotency_key, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s, %s, NULLIF(%s, ''), now(), now()) "
                "ON CONFLICT DO NOTHING",
                (
                    self._channel_id,
                    _id(request_id),
                    actor,
                    _id(target_tool_id),
                    _json(payload),
                    priority,
                    _PRIORITY_VALUES[priority],
                    deadline_at,
                    _id(idempotency_key) if idempotency_key else "",
                ),
            )
            self._notify(connection, request_id)
            connection.commit()

    def find_request_by_idempotency(
        self,
        token: str,
        target_tool_id: str,
        idempotency_key: str,
    ) -> dict[str, Any] | None:
        """Idempotent retry lookup: an existing request with the same key."""
        self._authorize(token, "request", target_tool_id)
        pool = self._get_pool()
        with pool.acquire() as connection:
            row = connection.execute(
                "SELECT request_id, status, response FROM gptbridge_transport.tool_request "
                "WHERE target_tool_id=%s AND idempotency_key=%s",
                (_id(target_tool_id), _id(idempotency_key)),
            ).fetchone()
        if row is None:
            return None
        return {
            "request_id": row["request_id"],
            "status": row["status"],
            "response": _decode(row["response"]),
        }

    def notify_channel(self, token: str, target_tool_id: str) -> None:
        self._authorize(token, "respond", target_tool_id)

    def cancel_request(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
    ) -> bool:
        actor = self._authorize(token, "cancel-request", target_tool_id)
        pool = self._get_pool()
        with pool.acquire() as connection:
            self._bind_identity(connection, actor, request_id)
            cursor = connection.execute(
                "UPDATE gptbridge_transport.tool_request "
                "SET status='cancelled', updated_at=now() "
                "WHERE channel_id=%s AND request_id=%s AND target_tool_id=%s "
                "AND requester_actor=%s AND status IN ('queued', 'claimed') "
                "RETURNING 1",
                (
                    self._channel_id,
                    _id(request_id),
                    _id(target_tool_id),
                    actor,
                ),
            )
            cancelled = cursor.fetchone() is not None
            if cancelled:
                self._notify(connection, request_id)
            connection.commit()
            return cancelled

    def request_cancelled(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
    ) -> bool:
        self._authorize(token, "claim", target_tool_id)
        pool = self._get_pool()
        with pool.acquire() as connection:
            row = connection.execute(
                "SELECT status FROM gptbridge_transport.tool_request "
                "WHERE channel_id=%s AND request_id=%s AND target_tool_id=%s",
                (
                    self._channel_id,
                    _id(request_id),
                    _id(target_tool_id),
                ),
            ).fetchone()
            return bool(row and row["status"] == "cancelled")

    def consume_response(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
    ) -> dict[str, Any] | None:
        actor = self._authorize(token, "consume-response", target_tool_id)
        pool = self._get_pool()
        with pool.acquire() as connection:
            connection.execute("BEGIN")
            self._bind_identity(connection, actor, request_id)
            row = connection.execute(
                "SELECT status, response, progress FROM gptbridge_transport.tool_request "
                "WHERE channel_id=%s AND request_id=%s AND target_tool_id=%s AND requester_actor=%s "
                "FOR UPDATE",
                (
                    self._channel_id,
                    _id(request_id),
                    _id(target_tool_id),
                    actor,
                ),
            ).fetchone()
            if not row:
                connection.execute("ROLLBACK")
                return None
            if row["status"] in ("completed", "cancelled"):
                connection.execute(
                    "DELETE FROM gptbridge_transport.tool_request "
                    "WHERE channel_id=%s AND request_id=%s",
                    (self._channel_id, _id(request_id)),
                )
            connection.commit()
        return {
            "status": row["status"],
            "response": _decode(row["response"]),
            "progress": _decode(row["progress"]),
        }

    def publish_progress(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        progress: Any,
    ) -> bool:
        actor = self._authorize(token, "respond", target_tool_id)
        pool = self._get_pool()
        with pool.acquire() as connection:
            self._bind_identity(connection, actor, request_id)
            cursor = connection.execute(
                "UPDATE gptbridge_transport.tool_request "
                "SET progress=%s, updated_at=now() "
                "WHERE channel_id=%s AND request_id=%s AND target_tool_id=%s AND status='claimed' "
                "RETURNING 1",
                (
                    _json(progress),
                    self._channel_id,
                    _id(request_id),
                    _id(target_tool_id),
                ),
            )
            updated = cursor.fetchone() is not None
            connection.commit()
            return updated
