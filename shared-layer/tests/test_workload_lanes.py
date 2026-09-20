"""§10.5 SQL workload lanes tests — bounded lanes, timeouts, slow-query log."""

from __future__ import annotations

import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from shared_layer.database.workload_lanes import (
    LanePolicy,
    PoolLaneExhausted,
    WorkloadClass,
    WorkloadLanePool,
)


class _FakeCursorResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self, delay_s: float = 0.0):
        self.delay_s = delay_s
        self.executed: list[str] = []
        self.closed = False

    class _Info:
        class _Tx:
            name = "IDLE"

        transaction_status = _Tx()

    info = _Info()

    def execute(self, sql, params=None):
        self.executed.append(str(sql))
        if self.delay_s:
            time.sleep(self.delay_s)
        return _FakeCursorResult([{"ok": True}])

    def rollback(self):
        pass

    def close(self):
        self.closed = True


class _FakeManager:
    def __init__(self, delay_s: float = 0.0):
        self.delay_s = delay_s
        self.conns: list[_FakeConn] = []

    @contextmanager
    def connection(self):
        conn = _FakeConn(self.delay_s)
        self.conns.append(conn)
        yield conn


def _pool(manager=None, lanes=None) -> WorkloadLanePool:
    return WorkloadLanePool(manager or _FakeManager(), lanes=lanes)


def test_three_lanes_have_distinct_policies():
    pool = _pool()
    with pool.connection(WorkloadClass.INTERACTIVE) as conn:
        conn.execute("SELECT 1")
    manager = pool._manager
    assert any("statement_timeout = 5000" in s for s in manager.conns[0].executed)


def test_migration_lane_no_statement_timeout():
    pool = _pool()
    with pool.connection(WorkloadClass.MIGRATION) as conn:
        conn.execute("SELECT 1")
    assert not any(
        "statement_timeout" in s for s in pool._manager.conns[0].executed
    )


def test_lane_bounded_wait_raises_on_exhaustion():
    lanes = {
        WorkloadClass.INTERACTIVE: LanePolicy(
            max_inflight=1,
            statement_timeout_ms=0,
            slow_query_ms=1000,
            queue_timeout_s=0.2,
        ),
        WorkloadClass.BACKGROUND: LanePolicy(1, 0, 1000, 30.0),
        WorkloadClass.MIGRATION: LanePolicy(1, 0, 1000, 30.0),
    }
    pool = _pool(lanes=lanes)
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with pool.connection(WorkloadClass.INTERACTIVE):
            entered.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    entered.wait(timeout=2)
    try:
        with pytest.raises(PoolLaneExhausted):
            with pool.connection(WorkloadClass.INTERACTIVE):
                pass
        stats = pool.stats()["interactive"]
        assert stats["queue_timeouts"] == 1
    finally:
        release.set()
        t.join(timeout=5)


def test_slow_query_counted():
    manager = _FakeManager(delay_s=0.05)
    lanes = {
        WorkloadClass.INTERACTIVE: LanePolicy(2, 0, 10, 5.0),
        WorkloadClass.BACKGROUND: LanePolicy(2, 0, 10, 5.0),
        WorkloadClass.MIGRATION: LanePolicy(1, 0, 10, 5.0),
    }
    pool = WorkloadLanePool(manager, lanes=lanes)
    pool.execute(WorkloadClass.BACKGROUND, "SELECT pg_sleep(1)")
    stats = pool.stats()["background"]
    assert stats["queries"] == 1
    assert stats["slow_queries"] == 1


def test_lanes_isolated_exhaustion():
    """背景 lane 滿不影響 interactive lane 取得連線。"""
    lanes = {
        WorkloadClass.INTERACTIVE: LanePolicy(1, 0, 1000, 5.0),
        WorkloadClass.BACKGROUND: LanePolicy(1, 0, 1000, 0.2),
        WorkloadClass.MIGRATION: LanePolicy(1, 0, 1000, 0.2),
    }
    pool = _pool(lanes=lanes)
    entered = threading.Event()
    release = threading.Event()

    def hold():
        with pool.connection(WorkloadClass.BACKGROUND):
            entered.set()
            release.wait(timeout=5)

    t = threading.Thread(target=hold)
    t.start()
    entered.wait(timeout=2)
    try:
        # interactive lane 仍可用
        pool.execute(WorkloadClass.INTERACTIVE, "SELECT 1")
        # background lane 逾時
        with pytest.raises(PoolLaneExhausted):
            with pool.connection(WorkloadClass.BACKGROUND):
                pass
    finally:
        release.set()
        t.join(timeout=5)


def test_stats_track_acquisitions_and_avg():
    pool = _pool()
    pool.execute(WorkloadClass.INTERACTIVE, "SELECT 1")
    pool.execute(WorkloadClass.INTERACTIVE, "SELECT 2")
    stats = pool.stats()["interactive"]
    assert stats["acquisitions"] == 2
    assert stats["queries"] == 2
    assert stats["inflight"] == 0
