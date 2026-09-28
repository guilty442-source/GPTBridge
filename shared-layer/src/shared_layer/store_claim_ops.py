"""Claim/respond-side operations mixin for PostgresSharedLayerStore.

Worker claiming uses ``FOR UPDATE SKIP LOCKED`` so multiple executors
share one queue; expired-lease reclaim covers crashed-worker recovery.
"""

from __future__ import annotations

from typing import Any

from .store_codec import decode as _decode, encode_json as _json
from .store_helpers import normalize_id as _id


class PostgresStoreClaimMixin:
    """Executor-side claim/respond operations for PostgresSharedLayerStore."""

    _channel_id: str

    def claim_request(
        self,
        token: str,
        target_tool_id: str,
        *,
        lease_duration_seconds: float = 300.0,
    ) -> dict[str, Any] | None:
        actor = self._authorize(token, "claim", target_tool_id)
        pool = self._get_pool()
        with pool.acquire() as connection:
            connection.execute("BEGIN")
            self._bind_identity(connection, actor)
            self._reclaim_expired_in_transaction(connection, target_tool_id)
            row = connection.execute(
                "SELECT request_id, requester_actor, payload FROM gptbridge_transport.tool_request "
                "WHERE channel_id=%s AND target_tool_id=%s AND status='queued' "
                "AND (next_retry_at IS NULL OR next_retry_at <= now()) "
                "AND (deadline_at IS NULL OR deadline_at > now()) "
                "ORDER BY priority_value, created_at, request_id "
                "LIMIT 1 FOR UPDATE SKIP LOCKED",
                (
                    self._channel_id,
                    _id(target_tool_id),
                ),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return None
            lease = connection.execute(
                "UPDATE gptbridge_transport.tool_request "
                "SET status='claimed', claimed_at=now(), "
                "lease_until=now() + (%s || ' seconds')::interval, "
                "attempt_count=attempt_count + 1, next_retry_at=NULL, updated_at=now() "
                "WHERE channel_id=%s AND request_id=%s AND status='queued' "
                "RETURNING lease_until, attempt_count",
                (str(float(lease_duration_seconds)), self._channel_id, row["request_id"]),
            ).fetchone()
            self._notify(connection, row["request_id"])
            connection.commit()
        return {
            "request_id": row["request_id"],
            "requester_actor": row["requester_actor"],
            "target_tool_id": target_tool_id,
            "payload": _decode(row["payload"]),
            "lease_until": lease["lease_until"] if lease else None,
            "attempt_count": lease["attempt_count"] if lease else 0,
        }

    def _reclaim_expired_in_transaction(self, connection: Any, target_tool_id: str) -> int:
        cursor = connection.execute(
            "UPDATE gptbridge_transport.tool_request "
            "SET status='queued', claimed_at=NULL, lease_until=NULL, "
            "next_retry_at=now(), updated_at=now() "
            "WHERE channel_id=%s AND target_tool_id=%s AND status='claimed' "
            "AND lease_until IS NOT NULL AND lease_until < now() RETURNING 1",
            (self._channel_id, _id(target_tool_id)),
        )
        return len(cursor.fetchall())

    def reclaim_expired(self, token: str, target_tool_id: str) -> int:
        """Re-queue requests whose lease expired (crashed worker recovery)."""
        self._authorize(token, "claim", target_tool_id)
        pool = self._get_pool()
        with pool.acquire() as connection:
            reclaimed = self._reclaim_expired_in_transaction(connection, target_tool_id)
            connection.commit()
            return reclaimed

    def respond(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        response: Any,
    ) -> bool:
        actor = self._authorize(token, "respond", target_tool_id)
        pool = self._get_pool()
        with pool.acquire() as connection:
            self._bind_identity(connection, actor, request_id)
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
