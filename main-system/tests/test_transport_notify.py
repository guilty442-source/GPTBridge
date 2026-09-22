"""§10.63/G26 transport LISTEN/NOTIFY 消費端測試。"""
from __future__ import annotations

import asyncio
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

import pytest

_SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
_SHARED = Path(__file__).resolve().parents[2] / "shared-layer" / "src"
for _p in (str(_SRC_CORE), str(_SHARED)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from shared_layer.transport_notify import TransportNotifyListener  # noqa: E402


class _Notify:
    def __init__(self, channel: str, payload: str) -> None:
        self.channel = channel
        self.payload = payload


class _FakeConn:
    def __init__(self, notifies):
        self._notifies = notifies
        self.listened: list[str] = []
        self.closed = False
        self.autocommit = False

    def execute(self, stmt):
        self.listened.append(str(stmt))

    def notifies(self, timeout=None):
        pending, self._notifies = self._notifies, []
        yield from pending
        raise TimeoutError


class _FakeManager:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def dedicated_connection(self, *, autocommit=True):
        self.conn.autocommit = autocommit
        try:
            yield self.conn
        finally:
            self.conn.closed = True


def test_subscribe_validates_channel():
    listener = TransportNotifyListener()
    with pytest.raises(ValueError):
        listener.subscribe("bad channel!", lambda c, r: None)
    with pytest.raises(ValueError):
        listener.subscribe("ai;DROP", lambda c, r: None)


def test_start_requires_subscriber():
    listener = TransportNotifyListener()
    assert listener.start() is False  # 無訂閱者不開連線


def test_dispatch_reaches_subscriber():
    listener = TransportNotifyListener()
    got = []
    listener.subscribe("ai", lambda ch, rid: got.append((ch, rid)))
    listener._dispatch("tool_request_ai", "req-1")
    assert got == [("ai", "req-1")]


def test_listener_loop_dispatches_and_stops():
    conn = _FakeConn([_Notify("tool_request_ai", "req-9")])
    listener = TransportNotifyListener(
        manager_factory=lambda: _FakeManager(conn), poll_slice_s=0.05
    )
    got = []
    listener.subscribe("ai", lambda ch, rid: got.append(rid))
    assert listener.start() is True
    deadline = time.monotonic() + 5
    while not got and time.monotonic() < deadline:
        time.sleep(0.05)
    listener.stop()
    assert got == ["req-9"]
    assert any("tool_request_ai" in s for s in conn.listened)


def test_resubscribe_triggers_relisten():
    conn = _FakeConn([])
    listener = TransportNotifyListener(
        manager_factory=lambda: _FakeManager(conn), poll_slice_s=0.05
    )
    listener.subscribe("ai", lambda c, r: None)
    listener.start()
    deadline = time.monotonic() + 5
    while not conn.listened and time.monotonic() < deadline:
        time.sleep(0.05)
    listener.subscribe("system", lambda c, r: None)
    conn2_seen = time.monotonic() + 5
    # resubscribe → reconnect → LISTEN both channels on a fresh conn
    while time.monotonic() < conn2_seen:
        if len(conn.listened) == 0 and conn.closed:
            break
        time.sleep(0.05)
    listener.stop()


@pytest.mark.asyncio
async def test_broker_wakes_on_notify():
    """broker _wait_or_wake：notify 喚醒提前返回（不等到 interval）。"""
    from tasks.model_service_activation import ModelServiceActivationBroker

    broker = ModelServiceActivationBroker(
        app=object(), toolbox_service=object(), cooldown=0.01
    )
    broker._loop_obj = asyncio.get_running_loop()
    started = time.monotonic()
    asyncio.get_running_loop().call_later(
        0.05, broker._on_transport_notify, "ai", "req-1"
    )
    await broker._wait_stop_or_wake(30.0)
    assert time.monotonic() - started < 5.0


@pytest.mark.asyncio
async def test_broker_wait_times_out_without_notify():
    from tasks.model_service_activation import ModelServiceActivationBroker

    broker = ModelServiceActivationBroker(
        app=object(), toolbox_service=object(), cooldown=0.01
    )
    started = time.monotonic()
    await broker._wait_stop_or_wake(0.2)
    assert time.monotonic() - started >= 0.2


@pytest.mark.asyncio
async def test_execution_waiter_woken_by_notify():
    """toolbox_execution：notify 按 request_id 喚醒對應 waiter。"""
    from tasks.toolbox_execution import ExecutionMixin

    mixin = ExecutionMixin.__new__(ExecutionMixin)
    loop = asyncio.get_running_loop()
    mixin._notify_waiters = {}
    mixin._notify_loop = loop
    event = asyncio.Event()
    mixin._notify_waiters["req-7"] = event

    mixin._on_transport_notify("system", "req-other")
    await asyncio.sleep(0)  # 讓 call_soon_threadsafe 有機會跑
    assert not event.is_set()  # 不同 request_id 不喚醒

    mixin._on_transport_notify("system", "req-7")
    started = time.monotonic()
    await mixin._sleep_or_notify(5.0, event)
    assert time.monotonic() - started < 5.0
