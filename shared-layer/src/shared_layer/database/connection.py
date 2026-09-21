from __future__ import annotations

import logging
from contextlib import contextmanager
from queue import Empty, Full, LifoQueue
from threading import Lock
from typing import Any, Iterator

from psycopg import Connection, connect
from psycopg.rows import dict_row

from ..security.dsn_policy import (
    DsnPolicyError,
    assert_no_admin_privileges,
    runtime_context_active,
)
from .config import DatabaseSettings

_logger = logging.getLogger("gptbridge.shared_layer.connection")


def database_dsn(admin_dsn: str, database: str) -> str:
    """Replace dbname without string-concatenating credentials."""
    return _dsn_with_db(admin_dsn, database)


def _dsn_with_db(dsn: str, database: str) -> str:
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    values = conninfo_to_dict(dsn)
    values["dbname"] = database
    return make_conninfo(**values)


class ConnectionManager:
    def __init__(self, settings: DatabaseSettings, *, min_size: int = 1, max_size: int = 8) -> None:
        self.settings = settings
        if min_size < 0 or max_size < 1 or min_size > max_size:
            raise ValueError("INVALID_CONNECTION_POOL_SIZE")
        # A501/A503: the runtime pool resolves the least-privilege runtime
        # binding; a declared runtime context fails closed without it and the
        # admin binding is never silently reused as the runtime credential.
        runtime_dsn = settings.runtime_dsn.strip()
        shared_binding = settings.shares_credential
        # A501: the role probe only applies to a *dedicated* runtime binding.
        # An identical admin/runtime binding is the legacy single-credential
        # posture — tolerated visibly (warning), never presented as separation.
        self._runtime_isolated = bool(runtime_dsn) and not shared_binding
        if not runtime_dsn:
            if runtime_context_active():
                raise DsnPolicyError("RUNTIME_DSN_REQUIRED_IN_RUNTIME_CONTEXT")
            runtime_dsn = settings.admin_dsn.strip()
        elif shared_binding:
            _logger.warning(
                "DSN_SHARED_CREDENTIAL_LEGACY_POSTURE: the runtime pool uses the "
                "admin binding; deploy a least-privilege GPTBRIDGE_POSTGRES_DSN "
                "runtime role (A501)"
            )
        self._dsn = database_dsn(runtime_dsn, settings.database)
        self._min_size = min_size
        self._max_size = max_size
        self._idle: LifoQueue[Connection[dict[str, Any]]] = LifoQueue(max_size)
        self._lock = Lock()
        self._connection_count = 0
        self._opened = False

    def _new_connection(self) -> Connection[dict[str, Any]]:
        connection = connect(self._dsn, row_factory=dict_row)
        if self._runtime_isolated and isinstance(connection, Connection):
            # A501/A506: a dedicated runtime binding must not carry elevated
            # role flags; the probe fails closed and the connection is closed.
            try:
                assert_no_admin_privileges(connection)
            except Exception:
                connection.close()
                raise
        return connection

    def open(self) -> None:
        with self._lock:
            if self._opened:
                return
            created: list[Connection[dict[str, Any]]] = []
            try:
                for _ in range(self._min_size):
                    created.append(self._new_connection())
            except Exception:
                for connection in created:
                    connection.close()
                raise
            for connection in created:
                self._idle.put_nowait(connection)
            self._connection_count = len(created)
            self._opened = True

    def set_max_size(self, max_size: int) -> None:
        """Adjust the pool ceiling (S7 adaptive-plane wiring).

        Growing lets ``connection()`` open more connections immediately;
        shrinking retires surplus connections lazily as they are returned.
        """
        with self._lock:
            if max_size < 1:
                raise ValueError("INVALID_CONNECTION_POOL_SIZE")
            self._max_size = int(max_size)

    @property
    def max_size(self) -> int:
        return self._max_size

    def stats(self) -> dict[str, int]:
        """Live pool counters for telemetry (G26 pool-usage observability)."""
        with self._lock:
            idle = self._idle.qsize()
            count = self._connection_count
            return {
                "pool_size": count,
                "pool_max": self._max_size,
                "idle_connections": idle,
                "active_connections": max(0, count - idle),
            }

    def close(self) -> None:
        with self._lock:
            self._opened = False
            while True:
                try:
                    connection = self._idle.get_nowait()
                except Empty:
                    break
                connection.close()
                self._connection_count -= 1

    @contextmanager
    def connection(self) -> Iterator[Connection[dict[str, Any]]]:
        if not self._opened:
            raise RuntimeError("CONNECTION_POOL_NOT_OPEN")
        try:
            connection = self._idle.get_nowait()
        except Empty:
            with self._lock:
                if self._connection_count >= self._max_size:
                    connection = self._idle.get(timeout=10)
                else:
                    connection = self._new_connection()
                    self._connection_count += 1
        try:
            yield connection
        finally:
            if not connection.closed and self._opened:
                if connection.info.transaction_status.name != "IDLE":
                    connection.rollback()
                # S7: retire on return when the adaptive bound shrank the
                # pool below the live count, or the idle queue is full
                # (queue capacity tracks the construction-time max).
                with self._lock:
                    over_cap = self._connection_count > self._max_size
                if over_cap:
                    connection.close()
                    with self._lock:
                        self._connection_count -= 1
                else:
                    try:
                        self._idle.put_nowait(connection)
                    except Full:
                        connection.close()
                        with self._lock:
                            self._connection_count -= 1
            else:
                if not connection.closed:
                    connection.close()
                with self._lock:
                    self._connection_count -= 1


_MANAGER: "ConnectionManager | None" = None
_MANAGER_LOCK = Lock()


def get_connection_manager() -> "ConnectionManager":
    """Lazy process-wide connection manager for maintenance telemetry."""
    global _MANAGER
    with _MANAGER_LOCK:
        if _MANAGER is None:
            manager = ConnectionManager(DatabaseSettings.from_environment())
            manager.open()
            _MANAGER = manager
        return _MANAGER


def peek_connection_manager() -> "ConnectionManager | None":
    """Return the live manager if one exists, without creating it (S7)."""
    with _MANAGER_LOCK:
        return _MANAGER


__all__ = [
    "ConnectionManager",
    "database_dsn",
    "get_connection_manager",
    "peek_connection_manager",
]
