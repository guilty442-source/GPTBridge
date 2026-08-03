from __future__ import annotations

from contextlib import contextmanager
from queue import Empty, LifoQueue
from threading import Lock
from typing import Any, Iterator

from psycopg import Connection, connect
from psycopg.rows import dict_row

from .config import DatabaseSettings


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
        self._dsn = database_dsn(settings.admin_dsn, settings.database)
        self._min_size = min_size
        self._max_size = max_size
        self._idle: LifoQueue[Connection[dict[str, Any]]] = LifoQueue(max_size)
        self._lock = Lock()
        self._connection_count = 0
        self._opened = False

    def _new_connection(self) -> Connection[dict[str, Any]]:
        return connect(self._dsn, row_factory=dict_row)

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
                self._idle.put(connection)
            else:
                if not connection.closed:
                    connection.close()
                with self._lock:
                    self._connection_count -= 1


__all__ = ["ConnectionManager", "database_dsn"]
