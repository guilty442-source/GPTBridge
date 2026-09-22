"""M2→M3 gate: A263 parity suite — replay the native scenario matrix against
the real Python authority.

`native/test_suites/suite_a263_channel_core.cpp` (a263_channel_core_suite.exe)
emits `native/test_suites/bin/a263_parity_matrix.json` with the C core's
observed verdicts for the three M2→M3 gate criteria:

  * heartbeat deadline  (heartbeat_mixin._heartbeat_loop predicate)
  * outbox 不丟未確認事件 (TransactionalOutbox append/fetch_after/replay_from)
  * cursor 收斂         (A263Channel ack advance + _handle_resync)

This test replays identical scenario inputs through the real Python objects
and compares key-by-key. Python is authoritative — any divergence fails the
test (fail-closed).
"""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

BIN = ROOT / "native" / "test_suites" / "bin"
EXE = BIN / "a263_channel_core_suite.exe"
MATRIX = BIN / "a263_parity_matrix.json"

from shared_layer.channel_runtime import A263Channel  # noqa: E402
from shared_layer.channel_types import ChannelConfig, MessagePriority  # noqa: E402
from shared_layer.heartbeat_mixin import HeartbeatMixin  # noqa: E402
from shared_layer.transactional_outbox import TransactionalOutbox  # noqa: E402


def _matrix() -> dict:
    if not EXE.exists():
        pytest.skip("a263_channel_core_suite.exe not built (run native/test_suites/build.ps1)")
    subprocess.run([str(EXE), "a263_channel_core_suite"], cwd=str(BIN),
                   check=True, capture_output=True, timeout=60)
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def _scenario(matrix: dict, name: str) -> dict:
    for entry in matrix["scenarios"]:
        if entry["scenario"] == name:
            return entry
    raise AssertionError(f"scenario {name} missing from matrix")


def _bare_channel(outbox=None) -> A263Channel:
    """A263Channel with only the fields _handle_control/_handle_resync touch."""
    ch = object.__new__(A263Channel)
    ch._acked_cursor = 0
    ch._sent_upto = 0
    ch.outbox = outbox
    ch._on_control = None
    return ch


async def _ack(ch: A263Channel, cursor: int) -> None:
    await ch._handle_control(
        {"type": "control", "command": "state_event_ack",
         "payload": {"cursor": cursor}},
        "state_event_ack",
    )


def test_parity_outbox_no_loss_replay():
    """Gate item 2: outbox never loses unconfirmed events (append →
    windowed fetch → resync replay covers every seq > cursor)."""
    row = _scenario(_matrix(), "outbox_no_loss_replay")

    async def run():
        ob = TransactionalOutbox(row["channel"])
        seqs, keys = [], []
        for i in range(len(row["appended"])):
            ev = await ob.append(f"e{i}", "entity", "op", {"i": i})
            seqs.append(ev.sequence)
            keys.append(ev.idempotency_key)
        assert seqs == row["appended"]
        assert keys == row["keys"]

        delivered = []
        cursor = 0
        while True:
            window = await ob.fetch_after(cursor, 4)
            if not window:
                break
            for ev in window:
                delivered.append(ev.sequence)
                if ev.sequence > cursor:
                    cursor = ev.sequence
        assert delivered == row["delivered"]

        # resync: real channel path resets both cursors, outbox replays.
        ch = _bare_channel(outbox=ob)
        ch._acked_cursor = cursor
        ch._sent_upto = cursor
        captured = []

        async def record(ev):
            captured.append(ev.sequence)

        ob._callback = record
        await ch._handle_resync(row["resync_cursor"])
        assert ch._acked_cursor == row["final_acked"]
        assert ch._sent_upto == row["final_acked"]
        assert captured == row["replayed"]

    asyncio.run(run())


def test_parity_cursor_convergence():
    """Gate item 3: ack cursor is monotonic; resync converges both cursors."""
    row = _scenario(_matrix(), "cursor_convergence")

    async def run():
        ch = _bare_channel()
        trajectory = []
        for ack in row["acks"]:
            await _ack(ch, ack)
            trajectory.append(ch._acked_cursor)
        assert trajectory == row["trajectory"]

        ch._sent_upto = 30
        await ch._handle_resync(row["resync_peer"])
        assert ch._acked_cursor == row["post_resync"]["acked"]
        assert ch._sent_upto == row["post_resync"]["sent"]

        post = []
        for ack in row["post_acks"]:
            await _ack(ch, ack)
            post.append(ch._acked_cursor)
        assert post == row["post_trajectory"]

    asyncio.run(run())


def test_parity_heartbeat_deadline():
    """Gate item 1: deadline predicate parity on the real mixin's attrs."""
    row = _scenario(_matrix(), "heartbeat_deadline")
    for r in row["rows"]:
        h = object.__new__(HeartbeatMixin)
        h._config = ChannelConfig(channel_id="ai",
                                  heartbeat_timeout_seconds=r["timeout"])
        h._last_pong_received = r["last_pong"]
        # exact predicate from HeartbeatMixin._heartbeat_loop
        expired = (
            r["now"] - h._last_pong_received
            > h.config.heartbeat_timeout_seconds
        )
        assert expired == bool(r["expired"]), r


class _HeartbeatProbe(HeartbeatMixin):
    """Minimal subclass so the real _heartbeat_loop can run end-to-end."""

    @property
    def generation(self):
        return types.SimpleNamespace(as_dict=lambda: {})

    async def _enqueue_control(self, message):
        return True


def test_parity_heartbeat_loop_live():
    """The real _heartbeat_loop fires (expired) and stays up (alive)."""

    async def run_once(delta: float, timeout: float) -> bool:
        h = _HeartbeatProbe()
        h._config = ChannelConfig(
            channel_id="ai",
            heartbeat_interval_seconds=0.005,
            heartbeat_timeout_seconds=timeout,
        )
        h._transport = types.SimpleNamespace()
        h._transport.close = _async_noop
        h._last_pong_received = time.monotonic() - delta
        try:
            await asyncio.wait_for(h._heartbeat_loop(), timeout=0.3)
        except asyncio.TimeoutError:
            return False  # loop still running -> alive
        return h._heartbeat_dead.is_set()

    async def _async_noop(*a, **k):
        return None

    async def main():
        expired = await run_once(delta=0.05, timeout=0.02)   # past deadline
        alive = await run_once(delta=0.01, timeout=30.0)     # within deadline
        return expired, alive

    expired, alive = asyncio.run(main())
    assert expired is True
    assert alive is False


RT_EXE = BIN / "channel_runtime_suite.exe"
RT_MATRIX = BIN / "a263_runtime_matrix.json"


class _FakeTransport:
    """ChannelTransport protocol stub — records sends, receive ends."""

    def __init__(self):
        self.sent: list[dict] = []
        self.closed: list[tuple] = []

    async def send(self, message):
        self.sent.append(message)

    async def receive(self):
        return None

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed.append((code, reason))

    @property
    def is_closed(self):
        return bool(self.closed)


def test_parity_channel_pipeline_runtime():
    """End-to-end replay of the C++ execution-plane pipeline matrix
    through a real A263Channel + TransactionalOutbox (M2→M3 gate)."""
    if not RT_EXE.exists():
        pytest.skip("channel_runtime_suite.exe not built")
    subprocess.run([str(RT_EXE), "channel_runtime_suite"], cwd=str(BIN),
                   check=True, capture_output=True, timeout=60)
    matrix = json.loads(RT_MATRIX.read_text(encoding="utf-8"))

    async def run():
        t = _FakeTransport()
        ob = TransactionalOutbox("ai")
        cfg = ChannelConfig(channel_id="ai",
                            heartbeat_interval_seconds=3600.0,
                            send_idle_sleep_seconds=0.01)
        ch = A263Channel(cfg, t, ob)

        for i in (1, 2, 3):
            await ob.append(f"e{i}", "T", "op", {"i": i})
        await ch._enqueue_message(MessagePriority.COMMAND, {"id": "m1"})
        await ch._enqueue_message(MessagePriority.CONTROL, {"id": "m2"})
        await ch._enqueue_message(MessagePriority.COMMAND, {"id": "m3"})
        await ch.connect("bg", "s")

        for _ in range(300):
            if len(t.sent) >= 7:
                break
            await asyncio.sleep(0.01)
        assert len(t.sent) >= 7

        traj = []
        for a in matrix["ack_input"]:
            await ch._dispatch_message(
                {"type": "control", "command": "state_event_ack",
                 "payload": {"cursor": a}})
            traj.append(ch._acked_cursor)

        before = len(t.sent)
        await ch._dispatch_message(
            {"type": "control", "command": "state_event_resync",
             "payload": {"cursor": matrix["resync_cursor"]}})
        for _ in range(300):
            if len(t.sent) >= before + 2:
                break
            await asyncio.sleep(0.01)

        await ch.disconnect()
        return t, traj, ch._acked_cursor

    t, traj, acked = asyncio.run(run())

    controls = [{"command": m.get("command")} for m in t.sent
                if m.get("type") == "control"]
    events = [{"sequence": m["event"]["sequence"]} for m in t.sent
              if m.get("type") == "state_event"]
    msgs = [{"id": m.get("id")} for m in t.sent if m.get("id")]
    assert controls == matrix["controls"]
    assert events == matrix["state_events"]
    assert msgs == matrix["messages"]
    assert traj == matrix["ack_trajectory"]
    assert acked == matrix["final_acked"]
