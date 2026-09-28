"""Async request-channel operations for PostgresSharedLayerStore.

Split from ``store_async`` (source-size contract): request claim/submit/
respond/consume async wrappers; push-channel wrappers live in
``store_async_push``.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .store_codec import decode as _decode, encode_json as _json
from .store_pool import _governed_connection
from .store_helpers import (
    _PRIORITY_VALUES,
    _normalize_priority_class,
    normalize_id as _id,
)


class PostgresStoreAsyncRequestMixin:
    """Async request submit/claim/respond/consume operations."""

    _channel_id: str

    def _get_pool(self):
        raise NotImplementedError

    def _authorize(self, token: str, action: str, target_tool_id: str) -> str:
        raise NotImplementedError

    def _notify(self, connection: Any, request_id: str) -> None:
        raise NotImplementedError

    async def aclaim_request(
        self,
        token: str,
        target_tool_id: str,
    ) -> dict[str, Any] | None:
        """Async version of claim_request for native async callers."""
        self._authorize(token, "claim", target_tool_id)
        pool = self._get_pool()
        return await asyncio.to_thread(self._claim_request_sync, pool, target_tool_id)

    def _claim_request_sync(self, pool, target_tool_id: str) -> dict[str, Any] | None:
        """Synchronous claim implementation for thread pool."""
        with _governed_connection(pool) as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT request_id, requester_actor, payload FROM gptbridge_transport.tool_request "
                "WHERE channel_id=%s AND target_tool_id=%s AND status='queued' "
                "AND (deadline_at IS NULL OR deadline_at > now()) "
                "ORDER BY priority_value, created_at, request_id "
                "LIMIT 1 FOR UPDATE SKIP LOCKED",
                (self._channel_id, _id(target_tool_id)),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return None
            connection.execute(
                "UPDATE gptbridge_transport.tool_request "
                "SET status='claimed', updated_at=now() "
                "WHERE channel_id=%s AND request_id=%s AND status='queued'",
                (self._channel_id, row["request_id"]),
            )
            self._notify(connection, row["request_id"])
            connection.commit()
        return {
            "request_id": row["request_id"],
            "requester_actor": row["requester_actor"],
            "target_tool_id": target_tool_id,
            "payload": _decode(row["payload"]),
        }

    async def asubmit_request(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        payload: Any,
        *,
        priority_class: str = "interactive",
        deadline_at: Any = None,
    ) -> None:
        """Async version of submit_request."""
        actor = self._authorize(token, "request", target_tool_id)
        priority = _normalize_priority_class(priority_class)
        pool = self._get_pool()
        await asyncio.to_thread(
            self._submit_request_sync,
            pool,
            actor,
            request_id,
            target_tool_id,
            payload,
            priority,
            deadline_at,
        )

    def _submit_request_sync(
        self,
        pool,
        actor,
        request_id,
        target_tool_id,
        payload,
        priority: str = "interactive",
        deadline_at: Any = None,
    ):
        with _governed_connection(pool) as connection:
            connection.execute(
                "INSERT INTO gptbridge_transport.tool_request "
                "(channel_id, request_id, requester_actor, target_tool_id, payload, status, "
                "priority_class, priority_value, deadline_at, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s, %s, now(), now())",
                (
                    self._channel_id,
                    _id(request_id),
                    actor,
                    _id(target_tool_id),
                    _json(payload),
                    priority,
                    _PRIORITY_VALUES[priority],
                    deadline_at,
                ),
            )
            self._notify(connection, request_id)
            connection.commit()

    async def arespond(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        response: Any,
    ) -> bool:
        """Async version of respond."""
        self._authorize(token, "respond", target_tool_id)
        pool = self._get_pool()
        return await asyncio.to_thread(
            self._respond_sync, pool, request_id, target_tool_id, response)

    def _respond_sync(self, pool, request_id, target_tool_id, response):
        with _governed_connection(pool) as connection:
            cursor = connection.execute(
                "UPDATE gptbridge_transport.tool_request "
                "SET status='completed', response=%s, updated_at=now() "
                "WHERE channel_id=%s AND request_id=%s AND target_tool_id=%s AND status='claimed' "
                "RETURNING 1",
                (
                    _json(response),
                    self._channel_id,
                    _id(request_id),
                    _id(target_tool_id),
                ),
            )
            completed = cursor.fetchone() is not None
            if completed:
                self._notify(connection, request_id)
            connection.commit()
            return completed

    async def aconsume_response(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
    ) -> dict[str, Any] | None:
        """Async version of consume_response."""
        actor = self._authorize(token, "consume-response", target_tool_id)
        pool = self._get_pool()
        return await asyncio.to_thread(
            self._consume_response_sync, pool, actor, request_id, target_tool_id)

    def _consume_response_sync(self, pool, actor, request_id, target_tool_id):
        with _governed_connection(pool) as connection:
            connection.execute("BEGIN")
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
                # A46/A448: completed rows move to the transport history —
                # never a silent DELETE.  The hot table stays bounded while
                # the ledger keeps the archived row.
                connection.execute(  # sql-ok: history ledger archives the whole request row verbatim; bounded by channel+request id
                    "INSERT INTO gptbridge_transport.tool_request_history "
                    "SELECT *, now() FROM gptbridge_transport.tool_request "
                    "WHERE channel_id=%s AND request_id=%s",
                    (self._channel_id, _id(request_id)),
                )
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
