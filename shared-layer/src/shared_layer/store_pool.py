"""Bounded psycopg connection pool for the governed transport store (C59).

Extracted from ``store.py`` under the source-size contract: the pool owns
bounded acquisition (``timeout`` wait on a full pool, ``_creating``
reservation against racing connects), idle-slot TTL reaping, and
deterministic session reset on release.  Idle conns carry an
``idle_since`` stamp so a daemon reaper can close backends a quiet
process no longer needs — the server-side slot count a process can hold
is bounded by ``max`` under load and decays to zero when idle.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Iterator

from .database.generation_fence import set_provenance
from .store_helpers import _POOL_IDLE_TTL_S, _QUERY_TIMEOUT


@contextmanager
def _governed_connection(pool) -> Iterator[Any]:
    """Pooled connection with the backend generation declared (A8/E21).

    The migration-016 fence only rejects stale writers when the connection
    declares its generation; every transport write path acquires connections
    through this helper so an unset declaration can never mean "allow".
    """
    with pool.acquire() as connection:
        set_provenance(connection)
        yield connection


class PgConnectionPool:
    """Thread-safe psycopg connection pool (see module docstring)."""

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
