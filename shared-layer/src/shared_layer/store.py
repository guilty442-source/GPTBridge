"""store — governed PostgreSQL transport (codex A8/A44/A49 + E30/E35).

The shared-layer request channel is now backed by ````gptbridge_transport.tool_request````
in the PostgreSQL central index.  The implementation uses ````pg_notify```` for
channel-level alerts and ````SELECT ... FOR UPDATE SKIP LOCKED```` for safe
concurrent claim operations.

PostgreSQL is the sole structured-data authority (A610/A621); the
``LocalSharedLayerStore`` SQLite fallback was retired with the migration
window and ``SharedLayerStore`` is the PostgreSQL variant only.
"""

from __future__ import annotations

import os
import threading
import time
import logging
from contextlib import contextmanager
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

from .store_async import PostgresStoreAsyncMixin
from .store_helpers import (
    _CHANS,
    _MAX_BYTES,
    _MAX_ID,
    _POOL_IDLE_TTL_S,
    _POOL_MAX_CONN,
    _POOL_MIN_CONN,
    _POOL_TIMEOUT,
    _PRIORITY_VALUES,
    _QUERY_TIMEOUT,
    _normalize_priority_class,
    decode as _decode,
    encode_json as _json,
    normalize_id as _id,
    now_iso as _now_iso,
)


_logger = logging.getLogger("gptbridge.shared_layer.store")


class _ConnectionPool:
    """Thread-safe psycopg connection pool.

    C59 pool accounting: checked-out conns are tracked in ``_in_use``;
    idle conns carry an ``idle_since`` stamp so a daemon reaper can close
    backends a quiet process no longer needs.  The server-side slot count
    a process can hold is therefore bounded by ``max`` under load and
    decays to zero when idle.
    """

    def __init__(self, dsn: str, min_conn: int, max_conn: int, timeout: float) -> None:
        self._dsn = dsn
        self._min = min_conn
        self._max = max_conn
        self._timeout = timeout
        self._pool: list[tuple[Any, float]] = []
        self._in_use: set[int] = set()
        self._creating = 0
        self._lock = threading.Lock()
        # Waiters block on this cond instead of 50ms polling — a released
        # conn wakes the next borrower immediately (perf: up to ~50ms saved
        # per contended acquire).
        self._cond = threading.Condition(self._lock)
        self._closed = False
        import psycopg
        # Pre-create minimum connections
        now = time.monotonic()
        for _ in range(min_conn):
            conn = psycopg.connect(
                dsn,
                row_factory=psycopg.rows.dict_row,
                connect_timeout=_QUERY_TIMEOUT,
                autocommit=False,
            )
            self._pool.append((conn, now))
        self._reaper = threading.Thread(
            target=self._reap_idle,
            name="pg-store-pool-reaper",
            daemon=True,
        )
        self._reaper.start()

    def _new_conn(self) -> Any:
        import psycopg
        return psycopg.connect(
            self._dsn,
            row_factory=psycopg.rows.dict_row,
            connect_timeout=_QUERY_TIMEOUT,
            autocommit=False,
        )

    def _reap_idle(self) -> None:
        # Low-CPU: wake at half the TTL; stale entries are closed while the
        # lock is not held.  A stale idle conn still counts toward ``max``
        # only until the next wake — bounded by construction.
        while True:
            time.sleep(max(5.0, _POOL_IDLE_TTL_S / 2.0))
            cutoff = time.monotonic() - _POOL_IDLE_TTL_S
            with self._lock:
                if self._closed:
                    return
                stale = [c for c, since in self._pool if since < cutoff]
                self._pool = [e for e in self._pool if e[1] >= cutoff]
            for conn in stale:
                try:
                    conn.close()
                except Exception:
                    pass

    def _checkout_idle(self) -> Any | None:
        """Pop the newest idle conn, discarding closed/stale entries."""
        while self._pool:
            conn, since = self._pool.pop()
            if conn.closed or conn.broken or time.monotonic() - since > _POOL_IDLE_TTL_S:
                try:
                    conn.close()
                except Exception:
                    pass
                continue
            return conn
        return None

    def _acquire_one(self) -> Any:
        """Bounded acquisition: idle reuse, in-cap create, else timed wait.

        The connect() handshake and the caller's borrow both run *outside*
        the pool lock — the previous shape yielded inside ``_lock`` which
        serialized every borrow and made the timeout path unreachable.
        ``_creating`` reserves a slot so racing creators cannot overshoot
        ``max``.
        """
        deadline = time.monotonic() + self._timeout
        reserved = False
        try:
            while True:
                with self._lock:
                    if self._closed:
                        raise RuntimeError("pool closed")
                    conn = self._checkout_idle()
                    if conn is not None:
                        self._in_use.add(id(conn))
                        return conn
                    if not reserved and (
                        len(self._in_use) + len(self._pool) + self._creating
                        < self._max
                    ):
                        self._creating += 1
                        reserved = True
                    elif not reserved:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError("connection pool exhausted")
                        # Condition wait: a released conn notifies the next
                        # waiter instantly; bounded by the deadline.
                        self._cond.wait(timeout=remaining)
                        continue
                if reserved:
                    conn = self._new_conn()
                    with self._lock:
                        self._creating -= 1
                        reserved = False
                        self._in_use.add(id(conn))
                    return conn
        except BaseException:
            if reserved:
                with self._lock:
                    self._creating -= 1
            raise

    @contextmanager
    def acquire(self):
        conn = self._acquire_one()
        try:
            yield conn
        finally:
            # Deterministic session reset (C59): a conn returned with an
            # open/aborted transaction must never leak INTRANS state to
            # the next borrower.
            try:
                if not conn.closed and conn.info.transaction_status.name != "IDLE":
                    conn.rollback()
            except Exception:
                pass
            with self._lock:
                self._in_use.discard(id(conn))
                # Perf: retain up to max (not min) to avoid
                # close/reconnect churn under bursty concurrency; the
                # idle reaper still bounds slot hold time.
                if not self._closed and len(self._pool) < self._max and not conn.closed:
                    self._pool.append((conn, time.monotonic()))
                    self._cond.notify()  # wake one waiter on the freed conn
                else:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def close_all(self) -> None:
        with self._lock:
            self._closed = True
            entries = self._pool
            self._pool = []
            self._in_use.clear()
            self._cond.notify_all()  # wake waiters so they see _closed
        for conn, _since in entries:
            try:
                conn.close()
            except Exception:
                pass


class PostgresSharedLayerStore(PostgresStoreAsyncMixin):
    """Governed PostgreSQL transport backed by gptbridge_transport.tool_request.

    Channel subscribers may LISTEN on ````tool_request_<channel_id>````; new rows
    are announced with pg_notify.  Concurrent claim operations use FOR UPDATE
    SKIP LOCKED so multiple executors can safely share one table.
    """

    _pool: _ConnectionPool | None = None
    _pool_lock = threading.Lock()

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
        if self._channel_id not in _CHANS:
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
        self._ensure_pool()

    def _build_dsn(self) -> str:
        try:
            from psycopg.conninfo import conninfo_to_dict, make_conninfo

            values = conninfo_to_dict(os.environ.get("GPTBRIDGE_POSTGRES_DSN", ""))
            if not values.get("dbname"):
                values["dbname"] = "gptbridge"
            return make_conninfo(**values)
        except Exception as exc:
            raise permission_denied() from exc

    @classmethod
    def _ensure_pool(cls) -> None:
        if cls._pool is None:
            with cls._pool_lock:
                if cls._pool is None:
                    # DSN is needed; defer actual creation to first instance
                    pass

    def _get_pool(self) -> _ConnectionPool:
        if PostgresSharedLayerStore._pool is None:
            with PostgresSharedLayerStore._pool_lock:
                if PostgresSharedLayerStore._pool is None:
                    PostgresSharedLayerStore._pool = _ConnectionPool(
                        self._dsn, _POOL_MIN_CONN, _POOL_MAX_CONN, _POOL_TIMEOUT
                    )
        return PostgresSharedLayerStore._pool

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

    def _bind_identity(
        self, connection: Any, actor: str, request_id: str = ""
    ) -> None:
        """Bind the short-term session identity to the current transaction.

        Every transport statement is attributed to the authenticated actor
        through ``set_config(..., local)`` (A501 session identity).  Binding is
        metadata: a failure is logged and never fails the transport itself.
        """
        try:
            from .security.session_identity import (
                SessionIdentity,
                apply_session_identity,
            )

            apply_session_identity(
                connection,
                SessionIdentity(
                    actor_id=str(actor or ""),
                    module_id=self._channel_id,
                    request_id=_id(request_id) if request_id else "",
                ),
            )
        except Exception:
            _logger.warning("session identity binding skipped", exc_info=True)

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


SharedLayerStore = PostgresSharedLayerStore

__all__ = [
    "PostgresSharedLayerStore",
    "SharedLayerStore",
]
