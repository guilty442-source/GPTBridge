"""codex_postgresql.readonly_connection bounded-slot behaviour (C59).

The codex read cache must never pin more backend slots than the process
cap, must release conns owned by dead threads, and must evict conns idle
past the TTL - the pre-fix thread-local cache leaked one slot per worker
thread for the whole process lifetime.
"""

from __future__ import annotations

import threading

import pytest

from governance_rule.execution import codex_postgresql as cp


class _FakeTransaction:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeConn:
    def __init__(self) -> None:
        self.closed = False
        self.broken = False
        self.closed_count = 0

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction()

    def execute(self, *_args, **_kwargs):
        return None

    def close(self) -> None:
        self.closed = True
        self.closed_count += 1


@pytest.fixture()
def fake_factory(monkeypatch):
    made: list[_FakeConn] = []

    def factory() -> _FakeConn:
        conn = _FakeConn()
        made.append(conn)
        return conn

    monkeypatch.setattr(cp, "_new_readonly_connection", factory)
    cp._thread_local.readonly_conn = None
    cp.close_cached_connections()  # drop real conns cached by earlier tests
    monkeypatch.setattr(cp, "_CODEX_CONN_CACHE_MAX", 3)
    monkeypatch.setattr(cp, "_CODEX_CONN_IDLE_TTL_S", 10_000.0)
    yield made
    cp.close_cached_connections()
    cp._thread_local.readonly_conn = None


def test_borrow_reuses_thread_local_conn(fake_factory):
    made = fake_factory
    for _ in range(3):
        with cp.readonly_connection():
            pass
    assert len(made) == 1
    assert len(cp._cached_connections) == 1


def test_cache_cap_falls_back_to_transient_conn(fake_factory):
    made = fake_factory

    def borrow_and_hold(barrier, held):
        with cp.readonly_connection():
            held.append(threading.get_ident())
            barrier.wait(timeout=10)

    barrier = threading.Barrier(4)  # 3 holders + main
    held: list[int] = []
    threads = [
        threading.Thread(target=borrow_and_hold, args=(barrier, held))
        for _ in range(3)
    ]
    for t in threads:
        t.start()
    barrier.wait(timeout=10)  # cap=3 reached: all cached
    assert len(cp._cached_connections) == 3
    # Fourth borrower (main thread) must get a transient conn that is
    # closed on exit rather than cached over the cap.
    with cp.readonly_connection():
        pass
    assert len(cp._cached_connections) == 3
    assert made[-1].closed
    for t in threads:
        t.join(timeout=10)


def test_dead_thread_conn_is_swept_on_next_borrow(fake_factory):
    made = fake_factory

    def borrow_once():
        with cp.readonly_connection():
            pass

    t = threading.Thread(target=borrow_once)
    t.start()
    t.join(timeout=10)
    dead_conn = made[0]
    assert not dead_conn.closed  # cached while thread was alive
    with cp.readonly_connection():
        pass
    assert dead_conn.closed
    assert len(made) == 2


def test_idle_ttl_evicts_cached_conn(fake_factory, monkeypatch):
    made = fake_factory
    monkeypatch.setattr(cp, "_CODEX_CONN_IDLE_TTL_S", 0.0)
    with cp.readonly_connection():
        pass
    assert len(cp._cached_connections) == 1
    with cp.readonly_connection():
        pass
    assert made[0].closed
    assert len(made) == 2


def test_close_cached_connections_releases_all(fake_factory):
    for _ in range(2):
        with cp.readonly_connection():
            pass
    cp.close_cached_connections()
    assert len(cp._cached_connections) == 0
    assert all(c.closed for c in fake_factory)


def test_broken_conn_is_discarded(fake_factory):
    with cp.readonly_connection():
        pass
    conn = fake_factory[0]
    conn.closed = True
    cp._thread_local.readonly_conn = (conn, 0.0)
    cp._cached_connections[id(conn)] = (conn, threading.get_ident(), 0.0)
    with cp.readonly_connection():
        pass
    assert len(fake_factory) == 2
    assert conn.closed_count >= 1
