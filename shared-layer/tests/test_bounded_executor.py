"""Bounded executor tests: capacity, priority, deadline, cancellation,
backpressure, drop/reject, governor quota adaptation, paused shedding."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

_SRC = str(Path(__file__).resolve().parents[1] / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from shared_layer.adaptive import (
    AdmissionRejected,
    AsyncBoundedExecutor,
    BoundedExecutor,
    OverflowPolicy,
    PoolPaused,
    PoolPolicy,
    PriorityClass,
    WorkExpired,
    class_quota,
)


def _policy(**over) -> PoolPolicy:
    base = dict(
        pool="test.pool",
        work_class="rag",
        min_workers=1,
        max_workers=2,
        queue_capacity=4,
        deadline_ms=5_000,
        overflow=OverflowPolicy.REJECT,
        backpressure_wait_ms=50,
    )
    base.update(over)
    return PoolPolicy(**base)


def _missing_state(tmp_path: Path) -> Path:
    return tmp_path / "no-state.json"




def _wait_for(pred, timeout: float = 5.0) -> bool:
    """Poll a predicate; avoids thread-startup races in tests."""
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _wait_running(ex, running: int = 1) -> None:
    assert _wait_for(lambda: ex.metrics()["running"] == running),         "worker did not pick up the blocker"

def _write_budget(path: Path, quota: int, state: str = "normal") -> None:
    path.write_text(
        json.dumps(
            {
                "contract": "resource-governor-state/v2",
                "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "interval": 20.0,
                "concurrency_budget": {
                    "contract": "concurrency-budget/v1",
                    "generation": 7,
                    "pressure": "none",
                    "classes": {
                        "rag": {"quota": quota, "state": state},
                    },
                },
            }
        ),
        encoding="utf-8",
    )


def test_submit_runs_on_pool_and_returns(tmp_path: Path) -> None:
    ex = BoundedExecutor(_policy(), state_path=_missing_state(tmp_path))
    try:
        assert ex.submit(lambda: 40 + 2).result(timeout=5) == 42
    finally:
        ex.shutdown()


def test_queue_capacity_rejects_overflow(tmp_path: Path) -> None:
    """capacity + reject: over-capacity admissions fail immediately."""
    gate = threading.Event()
    ex = BoundedExecutor(
        _policy(max_workers=1, queue_capacity=2),
        state_path=_missing_state(tmp_path),
    )
    try:
        f1 = ex.submit(gate.wait)
        _wait_running(ex)  # worker holds f1; heap is empty
        f2 = ex.submit(gate.wait)
        f3 = ex.submit(gate.wait)
        # worker holds f1; queue holds f2+f3 at capacity -> f4 rejected
        with pytest.raises(AdmissionRejected):
            ex.submit(gate.wait).result()
        gate.set()
        f1.result(timeout=5)
        f2.result(timeout=5)
        f3.result(timeout=5)
        assert ex.metrics()["rejected"] == 1
    finally:
        gate.set()
        ex.shutdown()


def test_priority_ordering(tmp_path: Path) -> None:
    """priority: queued critical work runs before earlier background."""
    gate = threading.Event()
    order: list[str] = []
    lock = threading.Lock()

    def record(name: str) -> str:
        with lock:
            order.append(name)
        return name

    ex = BoundedExecutor(
        _policy(max_workers=1, queue_capacity=8),
        state_path=_missing_state(tmp_path),
    )
    try:
        blocker = ex.submit(gate.wait, priority=PriorityClass.BACKGROUND)
        _wait_running(ex)  # single worker holds the blocker
        ex.submit(record, "bg", priority=PriorityClass.BACKGROUND)
        ex.submit(record, "crit", priority=PriorityClass.CRITICAL)
        ex.submit(record, "ui", priority=PriorityClass.INTERACTIVE)
        gate.set()
        blocker.result(timeout=5)
        deadline = time.time() + 5
        while len(order) < 3 and time.time() < deadline:
            time.sleep(0.05)
        assert order == ["crit", "ui", "bg"]
    finally:
        gate.set()
        ex.shutdown()


def test_deadline_expiry_sheds_queued_work(tmp_path: Path) -> None:
    """deadline: work that outlives its queue wait is expired, not run."""
    gate = threading.Event()
    ran = threading.Event()
    ex = BoundedExecutor(
        _policy(max_workers=1, queue_capacity=4),
        state_path=_missing_state(tmp_path),
    )
    try:
        blocker = ex.submit(gate.wait)
        _wait_running(ex)
        doomed = ex.submit(ran.set, deadline_ms=60)
        time.sleep(0.12)
        gate.set()
        blocker.result(timeout=5)
        with pytest.raises(WorkExpired):
            doomed.result(timeout=5)
        time.sleep(0.1)
        assert not ran.is_set()
        assert ex.metrics()["expired"] == 1
    finally:
        gate.set()
        ex.shutdown()


def test_cancel_event_retires_queued_work(tmp_path: Path) -> None:
    """cancellation: cancel_event sheds queued work before dispatch."""
    gate = threading.Event()
    cancel = threading.Event()
    ex = BoundedExecutor(
        _policy(max_workers=1, queue_capacity=4),
        state_path=_missing_state(tmp_path),
    )
    try:
        blocker = ex.submit(gate.wait)
        _wait_running(ex)
        victim = ex.submit(lambda: "x", cancel=cancel)
        cancel.set()
        gate.set()
        blocker.result(timeout=5)
        with pytest.raises(Exception):
            victim.result(timeout=5)
        assert ex.metrics()["cancelled"] >= 1
    finally:
        gate.set()
        ex.shutdown()


def test_backpressure_waits_for_capacity(tmp_path: Path) -> None:
    """backpressure: submit waits within wait_ms for room to open."""
    gate = threading.Event()
    ex = BoundedExecutor(
        _policy(max_workers=1, queue_capacity=1),
        state_path=_missing_state(tmp_path),
    )
    try:
        blocker = ex.submit(gate.wait)
        _wait_running(ex)  # worker holds blocker; heap has room
        queued = ex.submit(lambda: "q")
        holder: list = []
        waiter = threading.Thread(
            target=lambda: holder.append(
                ex.submit(lambda: "r", wait_ms=5_000)
            ),
            daemon=True,
        )
        waiter.start()
        time.sleep(0.15)
        assert not holder  # submit is blocked in backpressure, not rejected
        gate.set()
        waiter.join(timeout=5)
        blocker.result(timeout=5)
        assert queued.result(timeout=5) == "q"
        assert holder and holder[0].result(timeout=5) == "r"
    finally:
        gate.set()
        ex.shutdown()


def test_drop_oldest_evicts_lowest_priority(tmp_path: Path) -> None:
    """drop/reject policy: drop_oldest evicts the least important item."""
    gate = threading.Event()
    ex = BoundedExecutor(
        _policy(
            max_workers=1,
            queue_capacity=2,
            overflow=OverflowPolicy.DROP_OLDEST,
        ),
        state_path=_missing_state(tmp_path),
    )
    try:
        blocker = ex.submit(gate.wait)
        _wait_running(ex)
        bg = ex.submit(lambda: "bg", priority=PriorityClass.BACKGROUND)
        crit = ex.submit(lambda: "crit", priority=PriorityClass.CRITICAL)
        # heap now at capacity=2; next submit evicts lowest priority (bg)
        crit2 = ex.submit(lambda: "crit2", priority=PriorityClass.CRITICAL)
        gate.set()
        blocker.result(timeout=5)
        assert crit.result(timeout=5) == "crit"
        assert crit2.result(timeout=5) == "crit2"
        with pytest.raises(WorkExpired):
            bg.result(timeout=5)
        assert ex.metrics()["dropped"] == 1
    finally:
        gate.set()
        ex.shutdown()


def test_governor_quota_adapts_worker_count(tmp_path: Path) -> None:
    """adaptive: published class quota clamps the pool's worker count."""
    state = tmp_path / "state.json"
    _write_budget(state, quota=1)
    seen: list[str] = []
    lock = threading.Lock()

    def probe() -> str:
        with lock:
            seen.append(threading.current_thread().name)
        time.sleep(0.2)
        return "ok"

    ex = BoundedExecutor(
        _policy(max_workers=4, queue_capacity=8), state_path=state
    )
    try:
        futures = [ex.submit(probe) for _ in range(4)]
        for f in futures:
            assert f.result(timeout=10) == "ok"
        assert len(set(seen)) == 1  # quota=1: one worker drained all four
    finally:
        ex.shutdown()


def test_paused_class_rejects_admission(tmp_path: Path) -> None:
    """paused: governor-shed class rejects new admissions."""
    state = tmp_path / "state.json"
    _write_budget(state, quota=0, state="paused")
    ex = BoundedExecutor(_policy(), state_path=state)
    try:
        with pytest.raises(PoolPaused):
            ex.submit(lambda: 1).result()
    finally:
        ex.shutdown()


def test_class_quota_reads_published_budget(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    _write_budget(state, quota=3)
    quota = class_quota("rag", state)
