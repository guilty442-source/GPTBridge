"""Bounded acquisition operations for ConnectionManager (source-size split).

Contains the borrow/return path (``connection``), dedicated unpooled
subscribers (``dedicated_connection``), idle retrieval with TTL retirement
(``_take_idle``/``_retire``) and the adaptive-plane wait producer
(``_observe_plane_wait``).  Lifecycle and sizing stay in ``connection``.
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from queue import Empty, Full
from typing import Any, Iterator

from psycopg import Connection

# C59 POOL-ISOLATION: idle pooled backends past this TTL are closed on
# checkout so a quiet process does not hold server-side slots forever.
_IDLE_TTL_S = float(os.environ.get("GPTBRIDGE_CONN_POOL_IDLE_TTL_S", "120"))


class ConnectionAcquireMixin:
    """Borrow/return, dedicated subscribers and wait telemetry."""

    @contextmanager
    def dedicated_connection(
        self, *, autocommit: bool = True
    ) -> Iterator[Connection[dict[str, Any]]]:
        """Dedicated unpooled connection for long-lived subscribers
        (e.g. LISTEN/NOTIFY consumers).  Never drawn from the bounded
        pool — a held LISTEN conn must not starve query lanes — but still
        created through the governed DSN + privilege probe, and counted
        in stats for observability."""
        connection = self._new_connection()
        if autocommit:
            connection.autocommit = True
        with self._lock:
            self._dedicated_count += 1
        try:
            yield connection
        finally:
            if not connection.closed:
                connection.close()
            with self._lock:
                self._dedicated_count -= 1

    def _retire(self, connection: Connection[dict[str, Any]]) -> None:
        try:
            connection.close()
        except Exception:
            pass
        with self._lock:
            self._connection_count -= 1

    def _take_idle(self, timeout: float = 0.0) -> Connection[dict[str, Any]] | None:
        """Pop a live idle conn; stale/dead entries are retired (C59)."""
        deadline = time.monotonic() + timeout
        while True:
            try:
                entry = (
                    self._idle.get_nowait()
                    if timeout <= 0.0
                    else self._idle.get(timeout=max(0.0, deadline - time.monotonic()))
                )
            except Empty:
                return None
            connection, since = entry
            if (
                connection.closed
                or connection.broken
                or time.monotonic() - since > _IDLE_TTL_S
            ):
                self._retire(connection)
                if timeout > 0.0 and time.monotonic() >= deadline:
                    return None
                continue
            return connection

    @contextmanager
    def connection(self) -> Iterator[Connection[dict[str, Any]]]:
        if not self._opened:
            raise RuntimeError("CONNECTION_POOL_NOT_OPEN")
        connection = self._take_idle()
        if connection is None:
            with self._lock:
                at_cap = self._connection_count >= self._max_size
            if at_cap:
                start = time.monotonic()
                connection = self._take_idle(timeout=10)
                if connection is None:
                    with self._lock:
                        self._pool_wait_timeouts += 1
                    self._observe_plane_wait((time.monotonic() - start) * 1000.0)
                    raise Empty("connection pool exhausted")
                self._observe_plane_wait((time.monotonic() - start) * 1000.0)
            else:
                with self._lock:
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
                        self._idle.put_nowait((connection, time.monotonic()))
                    except Full:
                        connection.close()
                        with self._lock:
                            self._connection_count -= 1
            else:
                if not connection.closed:
                    connection.close()
                with self._lock:
                    self._connection_count -= 1

    def _observe_plane_wait(self, wait_ms: float) -> None:
        """P4 adaptive plane 生產者：pool 等待量測。

        ``pool_wait_timeouts``＝累計逾時次數（絕對值）；
        ``pg_wait_ms``＝最近一次滿池等待毫秒。欄位級合併，
        不覆寫其他生產者；失敗靜默——訊號只是提示，
        不得影響連線取得主流程。
        """
        try:
            from ..adaptive import LoadSignals, get_plane

            get_plane().observe_merge(
                LoadSignals(
                    pool_wait_timeouts=self._pool_wait_timeouts,
                    pg_wait_ms=wait_ms,
                ),
                fields=("pool_wait_timeouts", "pg_wait_ms"),
            )
        except Exception:
            pass
