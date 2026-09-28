"""Bounded read-only connection pool for Codex authority queries.

C59 POOL-ISOLATION: cached codex read connections are bounded two ways —
a per-process cap on held slots and an idle TTL that returns idle backends
to the server.  Workers that churn threads no longer leak one connection
per thread for the process lifetime (observed: every bound executor worker
pinned a runtime slot, exhausting non-superuser connections fleet-wide).
"""

from __future__ import annotations

import atexit
import os
import threading
import time
from contextlib import contextmanager
from typing import Any, Final, Iterator

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

try:
    from .codex_postgresql_dsn import CODEX_SCHEMA, runtime_dsn
except ImportError:  # flat script import (execution/ on sys.path)
    from codex_postgresql_dsn import CODEX_SCHEMA, runtime_dsn

_thread_local = threading.local()

_CODEX_CONN_IDLE_TTL_S: Final[float] = float(
    os.environ.get("GPTBRIDGE_CODEX_CONN_IDLE_TTL_S", "120")
)
_CODEX_CONN_CACHE_MAX: Final[int] = int(
    os.environ.get("GPTBRIDGE_CODEX_CONN_CACHE_MAX", "4")
)
# Registry: id(conn) -> (conn, owner_thread_ident, last_used_monotonic).
# A conn whose owning thread has exited is an orphan — it would hold a
# backend slot for the rest of the process lifetime, which is exactly how
# the slot exhaustion was observed.  Every borrow sweeps orphans.
_cached_connections: dict[int, tuple[psycopg.Connection[Any], int | None, float]] = {}
_cached_lock = threading.Lock()


def _close_connection(connection: psycopg.Connection[Any]) -> None:
    try:
        connection.close()
    except Exception:
        pass


def _drop_cached(connection: psycopg.Connection[Any]) -> None:
    with _cached_lock:
        _cached_connections.pop(id(connection), None)


def _sweep_cached() -> None:
    """Close orphaned/dead entries; must be called with _cached_lock held."""
    alive = {thread.ident for thread in threading.enumerate()}
    for key, (conn, owner, _last_used) in list(_cached_connections.items()):
        if conn.closed or conn.broken or owner not in alive:
            del _cached_connections[key]
            _close_connection(conn)


def close_cached_connections() -> None:
    """Close every cached codex read connection (process teardown/tests)."""
    with _cached_lock:
        connections = [entry[0] for entry in _cached_connections.values()]
        _cached_connections.clear()
    for connection in connections:
        _close_connection(connection)


atexit.register(close_cached_connections)


def _new_readonly_connection() -> psycopg.Connection[Any]:
    return psycopg.connect(
        runtime_dsn(),
        connect_timeout=5,
        options="-c default_transaction_read_only=on",
    )


def _borrow_cached() -> tuple[psycopg.Connection[Any] | None, bool]:
    """Return a reusable thread-local connection or None candidate."""
    entry = getattr(_thread_local, "readonly_conn", None)
    if entry is None:
        return None, False
    candidate, last_used = entry
    if candidate.closed or candidate.broken or (
        time.monotonic() - last_used > _CODEX_CONN_IDLE_TTL_S
    ):
        _drop_cached(candidate)
        _close_connection(candidate)
        return None, False
    return candidate, True


@contextmanager
def readonly_connection() -> Iterator[psycopg.Connection[Any]]:
    # Adjudication bursts issue several codex reads back-to-back; paying a
    # fresh psycopg.connect (~250 ms of socket handshake measured on the
    # event loop) per read starved the backend during startup.  Keep at
    # most one read-only connection per thread — bounded by the per-process
    # cache cap and an idle TTL — and wrap each borrow in a real
    # transaction, preserving the original single-snapshot semantics
    # (SET LOCAL still scopes search_path to the borrowed transaction).
    # When the cache cap is reached the borrow falls back to a transient
    # connection that is closed on exit: callers never observe unbounded
    # slot growth.
    connection, cached = _borrow_cached()
    if connection is None:
        # Connect outside the lock (~250 ms handshake must not serialize
        # first-time borrowers); the cap is enforced on registration.
        connection = _new_readonly_connection()
        with _cached_lock:
            _sweep_cached()
            cached = len(_cached_connections) < _CODEX_CONN_CACHE_MAX
            if cached:
                _cached_connections[id(connection)] = (
                    connection,
                    threading.get_ident(),
                    time.monotonic(),
                )
    try:
        with connection.transaction():
            connection.execute(
                sql.SQL("SET LOCAL search_path TO {}, pg_catalog").format(
                    sql.Identifier(CODEX_SCHEMA)
                )
            )
            yield connection
    finally:
        if connection.closed or connection.broken:
            _drop_cached(connection)
            _close_connection(connection)
            if getattr(_thread_local, "readonly_conn", (None,))[0] is connection:
                _thread_local.readonly_conn = None
        elif cached:
            _thread_local.readonly_conn = (connection, time.monotonic())
        else:
            _close_connection(connection)


def authority_state() -> dict[str, Any]:
    with readonly_connection() as connection:
        with connection.cursor(row_factory=dict_row) as cursor:
            row = cursor.execute(  # sql-ok: fixed authority-state table read (single metadata row)
                sql.SQL("SELECT * FROM {}.codex_authority_state").format(sql.Identifier(CODEX_SCHEMA))
            ).fetchone()
    if row is None:
        raise RuntimeError("POSTGRESQL_CODEX_AUTHORITY_NOT_INITIALIZED")
    return dict(row)
