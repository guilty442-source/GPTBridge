"""Bounded connection pool for ``pg_adapter`` (source-size split).

``psycopg.connect`` pays a full TCP + auth handshake + SET search_path on
every call; the governed repositories open a connection per operation, so
per-request latency was dominated by connect churn and each checkout also
spawned a short-lived PG backend (server-side RAM/CPU).  A bounded pool
keyed by (dsn, schema, autocommit, row_factory) turns each checkout into a
single loopback ``SELECT 1`` validation (~100x cheaper than a handshake)
while never mixing distinct bindings.

``PG_ADAPTER_POOL=0`` disables pooling outright (fail-open to the
per-call-connect behaviour); a dead checked-out connection is discarded
and a fresh one opened — callers never observe a broken pool entry.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

_POOL_MAX_SIZE = 4
# C59 POOL-ISOLATION: a pooled backend idle beyond this TTL is closed on
# checkout instead of being probed — quiet processes must not pin
# server-side slots indefinitely.
_POOL_IDLE_TTL_S = float(os.environ.get("GPTBRIDGE_PG_ADAPTER_IDLE_TTL_S", "120"))
# Perf: a conn idle for less than this interval is almost certainly live —
# skip the SELECT 1 loopback probe and hand it out directly (one RTT saved
# per hot-path checkout).
_POOL_PROBE_MIN_IDLE_S = float(
    os.environ.get("GPTBRIDGE_PG_ADAPTER_PROBE_MIN_IDLE_S", "2")
)
_pool_lock = threading.Lock()
_pool: dict[tuple[Any, ...], list[tuple[Any, float]]] = {}


def _pool_enabled() -> bool:
    return os.environ.get("PG_ADAPTER_POOL", "1") != "0"


def _pool_checkout(key: tuple[Any, ...]) -> Any | None:
    while True:
        with _pool_lock:
            entries = _pool.get(key)
            entry = entries.pop() if entries else None
        if entry is None:
            return None
        conn, since = entry
        idle_s = time.monotonic() - since
        try:
            if idle_s > _POOL_IDLE_TTL_S:
                raise TimeoutError("idle pool entry expired")
            if idle_s > _POOL_PROBE_MIN_IDLE_S:
                conn.execute("SELECT 1")  # loopback liveness probe
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            continue
        return conn


def _pool_release(key: tuple[Any, ...], conn: Any) -> None:
    try:
        # Session reset only when a transaction is actually open —
        # transaction_status is local state, so an IDLE conn skips the
        # rollback round trip entirely.
        if conn.info.transaction_status.name != "IDLE":
            conn.rollback()
    except Exception:
        pass
    try:
        if conn.closed:
            return
    except Exception:
        return
    with _pool_lock:
        entries = _pool.setdefault(key, [])
        if len(entries) < _POOL_MAX_SIZE:
            entries.append((conn, time.monotonic()))
            conn = None
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def close_pool() -> None:
    """Drop every pooled backend (process teardown / test isolation)."""
    with _pool_lock:
        entries = [conn for conns in _pool.values() for conn, _since in conns]
        _pool.clear()
    for conn in entries:
        try:
            conn.close()
        except Exception:
            pass
