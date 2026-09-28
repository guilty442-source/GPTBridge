"""Push-notification operations mixin for PostgresSharedLayerStore.

Push events ride the same ``tool_request`` table with status
``pushed`` -> ``claimed`` -> ``completed`` — no separate schema.
"""

from __future__ import annotations

from typing import Any

from .store_codec import decode as _decode, encode_json as _json
from .store_helpers import normalize_id as _id


class PostgresStorePushMixin:
    """One-way push delivery operations for PostgresSharedLayerStore."""

    _channel_id: str

    def submit_push(
        self,
        token: str,
        push_id: str,
        target_tool_id: str,
        payload: Any,
    ) -> None:
        """Deliver a one-way push notification from the sender to a target tool.

        Push events model main-system -> tool delivery (and tool -> tool on
        the same governed table).  They live on the same ``tool_request``
        table with status ``pushed`` -> ``claimed`` -> ``completed``, so no
        schema migration is required and existing request isolation holds.
        """
        actor = self._authorize(token, "request", target_tool_id)
        pool = self._get_pool()
        with pool.acquire() as connection:
            self._bind_identity(connection, actor, push_id)
            connection.execute(
                "INSERT INTO gptbridge_transport.tool_request "
                "(channel_id, request_id, requester_actor, target_tool_id, payload, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, 'pushed', now(), now())",
                (
                    self._channel_id,
                    _id(push_id),
                    actor,
                    _id(target_tool_id),
                    _json(payload),
                ),
            )
            self._notify(connection, push_id)
            connection.commit()

    def claim_pushed(
        self,
        token: str,
        target_tool_id: str,
    ) -> dict[str, Any] | None:
        """Claim a pushed notification addressed to this tool."""
        self._authorize(token, "claim", target_tool_id)
        pool = self._get_pool()
        with pool.acquire() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT request_id, requester_actor, payload FROM gptbridge_transport.tool_request "
                "WHERE channel_id=%s AND target_tool_id=%s AND status='pushed' "
                "ORDER BY created_at, request_id "
                "LIMIT 1 FOR UPDATE SKIP LOCKED",
                (
                    self._channel_id,
                    _id(target_tool_id),
                ),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return None
            connection.execute(
                "UPDATE gptbridge_transport.tool_request "
                "SET status='claimed', updated_at=now() "
                "WHERE channel_id=%s AND request_id=%s AND status='pushed'",
                (self._channel_id, row["request_id"]),
            )
            self._notify(connection, row["request_id"])
            connection.commit()
        return {
            "push_id": row["request_id"],
            "sender_actor": row["requester_actor"],
            "target_tool_id": target_tool_id,
            "payload": _decode(row["payload"]),
        }

    def acknowledge_push(
        self,
        token: str,
        push_id: str,
        target_tool_id: str,
    ) -> bool:
        """Acknowledge a claimed push so it is recorded as consumed."""
        self._authorize(token, "respond", target_tool_id)
        pool = self._get_pool()
        with pool.acquire() as connection:
            cursor = connection.execute(
                "UPDATE gptbridge_transport.tool_request "
                "SET status='completed', updated_at=now() "
                "WHERE channel_id=%s AND request_id=%s AND target_tool_id=%s AND status='claimed' "
                "RETURNING 1",
                (
                    self._channel_id,
                    _id(push_id),
                    _id(target_tool_id),
                ),
            )
            acknowledged = cursor.fetchone() is not None
            connection.commit()
            return acknowledged

    def status(self) -> dict[str, Any]:
        return {
            "engine": "postgresql",
            "table": "gptbridge_transport.tool_request",
            "channel_id": self._channel_id,
        }
