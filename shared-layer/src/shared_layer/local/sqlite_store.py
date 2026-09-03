"""sqlite_store — codex-native local transport store (stdlib sqlite3).

Interface-compatible replacement for ``shared_layer.store.SharedLayerStore``
that removes the PostgreSQL/psycopg dependency (A37/E23) while preserving the
same governed authorization semantics and method surface.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from governance_rule.execution.authentication import GovernanceAuthenticationService
from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
from governance_rule.permission_directory.execution.path_guard import permission_denied

_CHANNELS: Final = frozenset({"system", "ai"})
_MAX_ID: Final = 256
_MAX_BYTES: Final = 1_048_576
_QUERY_TIMEOUT: Final = 10.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _decode(value: str | None) -> Any:
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return value


def _local_database_relative(channel_id: str) -> str:
    policy = directory_authority_snapshot().shared_layer_access_policy
    declared = policy.ai_database_path if channel_id == "ai" else policy.database_path
    if not declared.startswith("local:"):
        raise permission_denied()
    relative = declared[len("local:"):].strip("/").replace("\\", "/")
    if not relative or ".." in relative.split("/"):
        raise permission_denied()
    return relative


class LocalSharedLayerStore:
    """Governed local transport used only by the local Python executor."""

    def __init__(self, project_root: Path | str, authentication: GovernanceAuthenticationService, channel_id: str = "system") -> None:
        if not isinstance(authentication, GovernanceAuthenticationService):
            raise permission_denied()
        self._project_root = Path(project_root).resolve()
        self._authentication = authentication
        self._channel_id = str(channel_id).strip().casefold()
        if self._channel_id not in _CHANNELS:
            raise permission_denied()
        self._database_path = (self._project_root / _local_database_relative(self._channel_id)).resolve()
        try:
            self._database_path.relative_to(self._project_root)
        except ValueError as exc:
            raise permission_denied() from exc
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=_QUERY_TIMEOUT,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """CREATE TABLE IF NOT EXISTS tool_request (
                       channel_id TEXT NOT NULL,
                       request_id TEXT NOT NULL,
                       requester_actor TEXT NOT NULL,
                       target_tool_id TEXT NOT NULL,
                       payload TEXT NOT NULL,
                       status TEXT NOT NULL DEFAULT 'queued',
                       response TEXT,
                       progress TEXT,
                       created_at TEXT NOT NULL,
                       updated_at TEXT NOT NULL,
                       PRIMARY KEY (channel_id, request_id)
                   )"""
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_request_claim "
                "ON tool_request (target_tool_id, status, created_at, request_id)"
            )

    @staticmethod
    def _id(value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized or len(normalized) > _MAX_ID or "\x00" in normalized:
            raise permission_denied()
        return normalized

    @staticmethod
    def _json(value: Any) -> str:
        try:
            encoded = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True)
        except (TypeError, ValueError) as exc:
            raise permission_denied() from exc
        if len(encoded.encode("utf-8")) > _MAX_BYTES:
            raise permission_denied()
        return encoded

    def _authorize(self, token: str, action: str, target_tool_id: str) -> str:
        claims = self._authentication.authenticate_token(token)
        capability = f"{self._channel_id}-channel-request-" + ("submit" if action in {"request", "cancel-request", "consume-response"} else "process")
        policy = directory_authority_snapshot().shared_layer_access_policy
        valid_resource_path = claims.resource_path in {
            policy.database_path,
            policy.ai_database_path,
        }
        if (claims.capability != capability or claims.action != action
                or claims.target != f"shared-layer-{self._channel_id}-request:{target_tool_id}"
                or claims.data_scope != f"shared-layer-{self._channel_id}-request"
                or not valid_resource_path):
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
        now = _now_iso()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO tool_request (channel_id,request_id,requester_actor,target_tool_id,payload,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (self._channel_id, self._id(request_id), actor, self._id(target_tool_id), self._json(payload), "queued", now, now),
            )

    def notify_channel(self, token: str, target_tool_id: str) -> None:
        self._authorize(token, "process", target_tool_id)

    def cancel_request(self, token: str, request_id: str, target_tool_id: str) -> bool:
        actor = self._authorize(token, "cancel-request", target_tool_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tool_request SET status='cancelled',updated_at=? WHERE channel_id=? AND request_id=? AND target_tool_id=? AND requester_actor=? AND status IN ('queued','claimed')",
                (_now_iso(), self._channel_id, self._id(request_id), target_tool_id, actor),
            )
            return cursor.rowcount == 1

    def request_cancelled(self, token: str, request_id: str, target_tool_id: str) -> bool:
        self._authorize(token, "claim", target_tool_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM tool_request WHERE channel_id=? AND request_id=? AND target_tool_id=?",
                (self._channel_id, self._id(request_id), target_tool_id),
            ).fetchone()
        return bool(row and row["status"] == "cancelled")

    def consume_response(self, token: str, request_id: str, target_tool_id: str) -> dict[str, Any] | None:
        actor = self._authorize(token, "consume-response", target_tool_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status,response,progress FROM tool_request WHERE channel_id=? AND request_id=? AND target_tool_id=? AND requester_actor=?",
                (self._channel_id, self._id(request_id), target_tool_id, actor),
            ).fetchone()
            if not row:
                connection.execute("ROLLBACK")
                return None
            if row["status"] in {"completed", "cancelled"}:
                connection.execute(
                    "DELETE FROM tool_request WHERE channel_id=? AND request_id=?",
                    (self._channel_id, request_id),
                )
            connection.execute("COMMIT")
        except sqlite3.DatabaseError:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise permission_denied() from None
        finally:
            connection.close()
        return {"status": row["status"], "response": _decode(row["response"]), "progress": _decode(row["progress"])}

    def publish_progress(self, token: str, request_id: str, target_tool_id: str, progress: Any) -> bool:
        self._authorize(token, "respond", target_tool_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tool_request SET progress=?,updated_at=? WHERE channel_id=? AND request_id=? AND target_tool_id=? AND status='claimed'",
                (self._json(progress), _now_iso(), self._channel_id, self._id(request_id), target_tool_id),
            )
            return cursor.rowcount == 1

    def claim_request(self, token: str, target_tool_id: str) -> dict[str, Any] | None:
        self._authorize(token, "claim", target_tool_id)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT channel_id,request_id,requester_actor,payload FROM tool_request WHERE channel_id=? AND target_tool_id=? AND status='queued' ORDER BY created_at,request_id LIMIT 1",
                (self._channel_id, target_tool_id),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return None
            claimed = connection.execute(
                "UPDATE tool_request SET status='claimed',updated_at=? WHERE channel_id=? AND request_id=? AND status='queued'",
                (_now_iso(), self._channel_id, row["request_id"]),
            )
            connection.execute("COMMIT")
            return {
                "request_id": row["request_id"],
                "requester_actor": row["requester_actor"],
                "target_tool_id": target_tool_id,
                "payload": _decode(row["payload"]),
            }
        except sqlite3.DatabaseError:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise permission_denied() from None
        finally:
            connection.close()

    def respond(self, token: str, request_id: str, target_tool_id: str, response: Any) -> bool:
        self._authorize(token, "respond", target_tool_id)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE tool_request SET status='completed',response=?,updated_at=? WHERE channel_id=? AND request_id=? AND target_tool_id=? AND status='claimed'",
                (self._json(response), _now_iso(), self._channel_id, self._id(request_id), target_tool_id),
            )
            return cursor.rowcount == 1


SharedLayerStore = LocalSharedLayerStore

__all__ = ["LocalSharedLayerStore", "SharedLayerStore"]