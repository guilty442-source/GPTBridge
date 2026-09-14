"""Async method mixin for PostgresSharedLayerStore (A185 split).

Contains the async wrappers (asubmit_request, aclaim_request,
arespond, aconsume_response) and their synchronous helpers that
run in the thread pool.
"""
from __future__ import annotations

import asyncio
from typing import Any

from .store_helpers import decode as _decode, normalize_id as _id, encode_json as _json


class PostgresStoreAsyncMixin:
    """Async wrappers for PostgresSharedLayerStore."""

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
        self._authorize(token, "process", target_tool_id)
        pool = self._get_pool()
        return await asyncio.to_thread(self._claim_request_sync, pool, target_tool_id)

    def _claim_request_sync(self, pool, target_tool_id: str) -> dict[str, Any] | None:
        """Synchronous claim implementation for thread pool."""
        with pool.acquire() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT request_id, requester_actor, payload FROM gptbridge_transport.tool_request "
                "WHERE channel_id=%s AND target_tool_id=%s AND status='queued' "
                "ORDER BY created_at, request_id "
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
    ) -> None:
        """Async version of submit_request."""
        actor = self._authorize(token, "request", target_tool_id)
        pool = self._get_pool()
        await asyncio.to_thread(
            self._submit_request_sync, pool, actor, request_id, target_tool_id, payload)

    def _submit_request_sync(self, pool, actor, request_id, target_tool_id, payload):
        with pool.acquire() as connection:
            connection.execute(
                "INSERT INTO gptbridge_transport.tool_request "
                "(channel_id, request_id, requester_actor, target_tool_id, payload, status, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, 'queued', now(), now())",
                (
                    self._channel_id,
                    _id(request_id),
                    actor,
                    _id(target_tool_id),
                    _json(payload),
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
        with pool.acquire() as connection:
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
        with pool.acquire() as connection:
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


__all__ = ["PostgresStoreAsyncMixin"]
