"""store — governed PostgreSQL transport (codex A8/A44/A49 + E30/E35).

The shared-layer request channel is now backed by ``gptbridge_transport.tool_request``
in the PostgreSQL central index.  The implementation uses ``pg_notify`` for
channel-level alerts and ``SELECT ... FOR UPDATE SKIP LOCKED`` for safe
concurrent claim operations.

``LocalSharedLayerStore`` remains available as a local fallback, but the default
``SharedLayerStore`` is the PostgreSQL variant.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from governance_rule.execution.authentication import (
    GovernanceAuthenticationService,
)
from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.execution.path_guard import (
    permission_denied,
)

from .local.sqlite_store import LocalSharedLayerStore

_CHANNELS: Final[frozenset[str]] = frozenset({"system", "ai"})
_MAX_ID: Final[int] = 256
_MAX_BYTES: Final[int] = 1_048_576
_QUERY_TIMEOUT: Final[float] = 10.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decode(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _json(value: Any) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as exc:
        raise permission_denied() from exc
    if len(encoded.encode("utf-8")) > _MAX_BYTES:
        raise permission_denied()
    return encoded


def _id(value: str) -> str:
    normalized = str(value or "").strip()
    if not normalized or len(normalized) > _MAX_ID or "\x00" in normalized:
        raise permission_denied()
    return normalized


class PostgresSharedLayerStore:
    """Governed PostgreSQL transport backed by gptbridge_transport.tool_request.

    Channel subscribers may LISTEN on ``tool_request_<channel_id>``; new rows
    are announced with pg_notify.  Concurrent claim operations use FOR UPDATE
    SKIP LOCKED so multiple executors can safely share one table.
    """

    def __init__(
        self,
        project_root: Path | str,
        authentication: GovernanceAuthenticationService,
        channel_id: str = "system",
    ) -> None:
        if not isinstance(authentication, GovernanceAuthenticationService):
            raise permission_denied()
        self._authentication = authentication
        self._project_root = Path(project_root).resolve()
        self._channel_id = str(channel_id or "").strip().casefold()
        if self._channel_id not in _CHANNELS:
            raise permission_denied()
        policy = directory_authority_snapshot().shared_layer_access_policy
        declared = (
            policy.ai_database_path
            if self._channel_id == "ai"
            else policy.database_path
        )
        if not declared.startswith("postgresql:"):
            raise permission_denied()
        self._resource_path = declared
        self._dsn = self._build_dsn()

    def _build_dsn(self) -> str:
        from shared_layer.database.config import DatabaseSettings

        settings = DatabaseSettings.from_environment()
        try:
            from psycopg.conninfo import conninfo_to_dict, make_conninfo

            values = conninfo_to_dict(settings.admin_dsn)
            values["dbname"] = settings.database
            return make_conninfo(**values)
        except Exception as exc:
            raise permission_denied() from exc

    def _connect(self) -> Any:
        import psycopg

        return psycopg.connect(
            self._dsn,
            row_factory=psycopg.rows.dict_row,
            connect_timeout=_QUERY_TIMEOUT,
        )

    def _authorize(self, token: str, action: str, target_tool_id: str) -> str:
        claims = self._authentication.authenticate_token(token)
        capability = f"{self._channel_id}-channel-request-" + (
            "submit" if action in {"request", "cancel-request", "consume-response"} else "process"
        )
        policy = directory_authority_snapshot().shared_layer_access_policy
        valid_resource_path = claims.resource_path in {
            policy.database_path,
            policy.ai_database_path,
        }
        if (
            claims.capability != capability
            or claims.action != action
            or claims.target != f"shared-layer-{self._channel_id}-request:{target_tool_id}"
            or claims.data_scope != f"shared-layer-{self._channel_id}-request"
            or not valid_resource_path
        ):
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

    def _notify(self, connection: Any, request_id: str) -> None:
        connection.execute(
            "SELECT pg_notify(%s, %s)",
            (f"tool_request_{self._channel_id}", _id(request_id)),
        )

    def submit_request(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        payload: Any,
    ) -> None:
        actor = self._authorize(token, "request", target_tool_id)
        with self._connect() as connection:
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

    def notify_channel(self, token: str, target_tool_id: str) -> None:
        self._authorize(token, "process", target_tool_id)

    def cancel_request(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
    ) -> bool:
        actor = self._authorize(token, "cancel-request", target_tool_id)
        with self._connect() as connection:
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
        with self._connect() as connection:
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
        with self._connect() as connection:
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

    def publish_progress(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        progress: Any,
    ) -> bool:
        self._authorize(token, "respond", target_tool_id)
        with self._connect() as connection:
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

    def claim_request(
        self,
        token: str,
        target_tool_id: str,
    ) -> dict[str, Any] | None:
        self._authorize(token, "process", target_tool_id)
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT request_id, requester_actor, payload FROM gptbridge_transport.tool_request "
                "WHERE channel_id=%s AND target_tool_id=%s AND status='queued' "
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

    def respond(
        self,
        token: str,
        request_id: str,
        target_tool_id: str,
        response: Any,
    ) -> bool:
        self._authorize(token, "respond", target_tool_id)
        with self._connect() as connection:
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

    def status(self) -> dict[str, Any]:
        return {
            "engine": "postgresql",
            "table": "gptbridge_transport.tool_request",
            "channel_id": self._channel_id,
        }


SharedLayerStore = PostgresSharedLayerStore

__all__ = [
    "LocalSharedLayerStore",
    "PostgresSharedLayerStore",
    "SharedLayerStore",
]
