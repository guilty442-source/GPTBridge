from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Final

try:
    import orjson as _orjson

    def _dumps(obj: Any) -> str:
        return _orjson.dumps(obj, option=_orjson.OPT_NON_STR_KEYS | _orjson.OPT_SORT_KEYS).decode("utf-8")

except ImportError:
    def _dumps(obj: Any) -> str:
        return json.dumps(obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from governance_rule.execution.authentication import GovernanceAuthenticationService
from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
from governance_rule.permission_directory.execution.path_guard import permission_denied

_CHANNELS: Final = frozenset({"system", "ai"})
_MAX_ID: Final = 256
_MAX_BYTES: Final = 1_048_576
_POOL_MIN: Final = 2
_POOL_MAX: Final = 8

_pools: dict[str, ConnectionPool[dict[str, Any]]] = {}


class SharedLayerStore:
    """Governed PostgreSQL transport used only by the local Python executor."""

    def __init__(self, project_root: Path | str, authentication: GovernanceAuthenticationService, channel_id: str = "system") -> None:
        if not isinstance(authentication, GovernanceAuthenticationService):
            raise permission_denied()
        self._project_root = Path(project_root).resolve()
        self._authentication = authentication
        self._channel_id = str(channel_id).strip().casefold()
        if self._channel_id not in _CHANNELS:
            raise permission_denied()
        policy = directory_authority_snapshot().shared_layer_access_policy
        self._database_relative = policy.ai_database_path if self._channel_id == "ai" else policy.database_path
        self._dsn = str(os.environ.get("GPTBRIDGE_POSTGRES_DSN") or "").strip()
        if not self._dsn:
            raise permission_denied()
        if self._dsn not in _pools:
            _pools[self._dsn] = ConnectionPool(
                self._dsn,
                min_size=_POOL_MIN,
                max_size=_POOL_MAX,
                kwargs={"row_factory": dict_row},
            )
        self._pool = _pools[self._dsn]
        try:
            with self._pool.connection() as connection:
                row = connection.execute(
                    "SELECT to_regclass('gptbridge_transport.tool_request') AS transport"
                ).fetchone()
            if not row or row["transport"] is None:
                raise permission_denied()
        except psycopg.Error as exc:
            raise permission_denied() from exc

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return self._pool.connection()

    @staticmethod
    def _id(value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > _MAX_ID or "\x00" in normalized:
            raise permission_denied()
        return normalized

    @staticmethod
    def _json(value: Any) -> str:
        try:
            encoded = _dumps(value)
        except (TypeError, ValueError) as exc:
            raise permission_denied() from exc
        if len(encoded.encode("utf-8")) > _MAX_BYTES:
            raise permission_denied()
        return encoded

    def _authorize(self, token: str, action: str, target_tool_id: str) -> str:
        claims = self._authentication.authenticate_token(token)
        capability = f"{self._channel_id}-channel-request-" + ("submit" if action in {"request", "cancel-request", "consume-response"} else "process")
        if (claims.capability != capability or claims.action != action
                or claims.target != f"shared-layer-{self._channel_id}-request:{target_tool_id}"
                or claims.data_scope != f"shared-layer-{self._channel_id}-request"
                or claims.resource_path != self._database_relative):
            raise permission_denied()
        if action in {"request", "cancel-request", "consume-response"}:
            if claims.target_tool_id != target_tool_id and not (
                claims.target_tool_id is None
                and claims.bound_tool_id == target_tool_id
            ):
                raise permission_denied()
        elif claims.target_tool_id is not None or claims.bound_tool_id != target_tool_id:
            raise permission_denied()
        return claims.actor

    def submit_request(self, token: str, request_id: str, target_tool_id: str, payload: Any) -> None:
        actor = self._authorize(token, "request", target_tool_id)
        try:
            with self._pool.connection() as connection:
                connection.execute("INSERT INTO gptbridge_transport.tool_request (channel_id,request_id,requester_actor,target_tool_id,payload,status) VALUES (%s,%s,%s,%s,%s,'queued')", (self._channel_id, self._id(request_id), actor, self._id(target_tool_id), self._json(payload)))
                connection.execute(
                    "SELECT pg_notify('gptbridge_tool_request', %s)",
                    (f"{self._channel_id}:{self._id(target_tool_id)}",),
                )
        except psycopg.Error as exc:
            raise permission_denied() from exc

    def notify_channel(self, token: str, target_tool_id: str) -> None:
        self._authorize(token, "process", target_tool_id)
        try:
            with self._pool.connection() as connection:
                connection.execute(
                    "SELECT pg_notify('gptbridge_tool_request', %s)",
                    (f"{self._channel_id}:{self._id(target_tool_id)}",),
                )
        except psycopg.Error as exc:
            raise permission_denied() from exc

    def cancel_request(self, token: str, request_id: str, target_tool_id: str) -> bool:
        actor = self._authorize(token, "cancel-request", target_tool_id)
        try:
            with self._connect() as connection:
                cursor = connection.execute("UPDATE gptbridge_transport.tool_request SET status='cancelled',updated_at=now() WHERE channel_id=%s AND request_id=%s AND target_tool_id=%s AND requester_actor=%s AND status IN ('queued','claimed')", (self._channel_id, self._id(request_id), target_tool_id, actor))
                return cursor.rowcount == 1
        except psycopg.Error as exc:
            raise permission_denied() from exc

    def request_cancelled(self, token: str, request_id: str, target_tool_id: str) -> bool:
        self._authorize(token, "claim", target_tool_id)
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT status FROM gptbridge_transport.tool_request WHERE channel_id=%s AND request_id=%s AND target_tool_id=%s", (self._channel_id, self._id(request_id), target_tool_id)).fetchone()
            return bool(row and row["status"] == "cancelled")
        except psycopg.Error as exc:
            raise permission_denied() from exc

    def consume_response(self, token: str, request_id: str, target_tool_id: str) -> dict[str, Any] | None:
        actor = self._authorize(token, "consume-response", target_tool_id)
        try:
            with self._connect() as connection:
                row = connection.execute("SELECT status,response,progress FROM gptbridge_transport.tool_request WHERE channel_id=%s AND request_id=%s AND target_tool_id=%s AND requester_actor=%s", (self._channel_id, self._id(request_id), target_tool_id, actor)).fetchone()
                if not row:
                    return None
                if row["status"] in {"completed", "cancelled"}:
                    connection.execute("DELETE FROM gptbridge_transport.tool_request WHERE channel_id=%s AND request_id=%s", (self._channel_id, request_id))
            return {"status": row["status"], "response": row["response"], "progress": row["progress"]}
        except psycopg.Error as exc:
            raise permission_denied() from exc

    def publish_progress(self, token: str, request_id: str, target_tool_id: str, progress: Any) -> bool:
        self._authorize(token, "respond", target_tool_id)
        try:
            with self._connect() as connection:
                cursor = connection.execute("UPDATE gptbridge_transport.tool_request SET progress=%s,updated_at=now() WHERE channel_id=%s AND request_id=%s AND target_tool_id=%s AND status='claimed'", (self._json(progress), self._channel_id, self._id(request_id), target_tool_id))
                return cursor.rowcount == 1
        except psycopg.Error as exc:
            raise permission_denied() from exc

    def claim_request(self, token: str, target_tool_id: str) -> dict[str, Any] | None:
        self._authorize(token, "claim", target_tool_id)
        try:
            with self._connect() as connection:
                row = connection.execute("WITH candidate AS (SELECT channel_id,request_id FROM gptbridge_transport.tool_request WHERE channel_id=%s AND target_tool_id=%s AND status='queued' ORDER BY created_at,request_id FOR UPDATE SKIP LOCKED LIMIT 1) UPDATE gptbridge_transport.tool_request AS r SET status='claimed',updated_at=now() FROM candidate WHERE r.channel_id=candidate.channel_id AND r.request_id=candidate.request_id RETURNING r.request_id,r.requester_actor,r.payload", (self._channel_id, target_tool_id)).fetchone()
            return None if not row else {"request_id": row["request_id"], "requester_actor": row["requester_actor"], "target_tool_id": target_tool_id, "payload": row["payload"]}
        except psycopg.Error as exc:
            raise permission_denied() from exc

    def respond(self, token: str, request_id: str, target_tool_id: str, response: Any) -> bool:
        self._authorize(token, "respond", target_tool_id)
        try:
            with self._connect() as connection:
                cursor = connection.execute("UPDATE gptbridge_transport.tool_request SET status='completed',response=%s,updated_at=now() WHERE channel_id=%s AND request_id=%s AND target_tool_id=%s AND status='claimed'", (self._json(response), self._channel_id, self._id(request_id), target_tool_id))
                return cursor.rowcount == 1
        except psycopg.Error as exc:
            raise permission_denied() from exc
