from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))

from shared_layer.channel_runtime import A263Channel, create_channel
from shared_layer.channel_types import ChannelConfig, MessagePriority
from shared_layer.transactional_outbox import TransactionalOutbox


class _LoopTransport:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self._closed = False

    async def send(self, message: dict) -> None:
        self.sent.append(message)

    async def receive(self) -> dict:
        await asyncio.sleep(3600)
        return dict()

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed


def test_send_loop_drains_queues_and_outbox() -> None:
    async def scenario() -> None:
        transport = _LoopTransport()
        config = ChannelConfig(
            channel_id="system",
            heartbeat_interval_seconds=60.0,
            heartbeat_timeout_seconds=60.0,
            send_idle_sleep_seconds=0.001,
        )
        channel = await create_channel("system", transport, config, TransactionalOutbox("system"))
        await channel.send({"type": "message", "command": "ping"})
        await channel.append_state_event(
            entity_id="e1",
            entity_type="resource",
            operation="update",
            payload={"x": 1},
        )
        await asyncio.sleep(0.05)
        await channel.disconnect()

        control = [
            m for m in transport.sent if m.get("type") == "control"
        ]
        state_events = [m for m in transport.sent if m.get("type") == "state_event"]
        assert any("state_event_hello" in str(m) for m in control)
        assert any(m.get("command") == "ping" for m in transport.sent)
        assert state_events
        assert state_events[-1]["event"]["operation"] == "update"
        assert state_events[-1]["event"]["sequence"] == 1

    asyncio.run(scenario())


def test_send_loop_respects_control_priority() -> None:
    async def scenario() -> None:
        transport = _LoopTransport()
        config = ChannelConfig(
            channel_id="system",
            heartbeat_interval_seconds=60.0,
            heartbeat_timeout_seconds=60.0,
            send_idle_sleep_seconds=0.001,
        )
        channel = await create_channel("system", transport, config)
        await channel.send({"type": "message", "command": "heavy"}, MessagePriority.COMMAND)
        await channel._enqueue_control({"type": "control", "command": "state_event_ack", "payload": {"cursor": 9}})
        await asyncio.sleep(0.05)
        await channel.disconnect()
        sent = transport.sent
        ack_index = next(
            i for i, m in enumerate(sent)
            if m.get("command") == "state_event_ack"
        )
        heavy_index = next(
            i for i, m in enumerate(sent)
            if m.get("command") == "heavy"
        )
        assert ack_index < heavy_index

    asyncio.run(scenario())


def test_outbox_callback_and_replay() -> None:
    async def scenario() -> None:
        received: list = []
        outbox = TransactionalOutbox("system", callback=lambda ev: received.append(ev))
        event = await outbox.append("e2", "resource", "create", {"k": "v"})
        assert event.sequence == 1
        await outbox.replay_from(0)
        assert len(received) >= 1

    asyncio.run(scenario())