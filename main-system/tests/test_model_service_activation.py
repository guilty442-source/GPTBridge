"""ModelServiceActivationBroker — §10.64 admission hold + auto-release."""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

_SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
if str(_SRC_CORE) not in sys.path:
    sys.path.insert(0, str(_SRC_CORE))

from tasks.model_service_activation import (  # noqa: E402
    OWNER_TOOL_ID,
    ModelServiceActivationBroker,
)


class _ToolboxStub:
    def __init__(self, *, running: bool = False) -> None:
        self.running = running
        self.starts: list[dict] = []
        self.stops: list[dict] = []

    async def tool_process_active(self, tool_id: str) -> bool:
        assert tool_id == OWNER_TOOL_ID
        return self.running

    async def start_tool(self, payload: dict) -> dict:
        self.starts.append(payload)
        self.running = True
        return {"ok": True, "pid": 4242}

    async def stop_tool(self, payload: dict) -> dict:
        self.stops.append(payload)
        self.running = False
        return {"ok": True}


def _broker(toolbox: _ToolboxStub) -> ModelServiceActivationBroker:
    return ModelServiceActivationBroker(
        app=object(), toolbox_service=toolbox, cooldown=0.01
    )


def _pending(broker: ModelServiceActivationBroker, value: bool) -> None:
    broker._has_pending_dialogue_request = lambda: value  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_hold_blocks_start_and_proceeds_without(monkeypatch) -> None:
    import tasks.model_service_activation as mod

    hold = {"on": True}
    monkeypatch.setattr(
        mod, "worker_admission_hold", lambda: hold["on"]
    )
    toolbox = _ToolboxStub()
    broker = _broker(toolbox)
    _pending(broker, True)
    decision = await broker._ensure_inner()
    assert decision == "resource-hold"
    assert toolbox.starts == []

    hold["on"] = False
    decision = await broker._ensure_inner()
    assert decision == "started"
    assert len(toolbox.starts) == 1
    assert broker._broker_started_owner is True


@pytest.mark.asyncio
async def test_release_only_broker_started_owner_under_regulation(
    monkeypatch,
) -> None:
    import tasks.model_service_activation as mod

    regulating = {"on": True}
    monkeypatch.setattr(
        mod, "regulation_active", lambda: regulating["on"]
    )

    toolbox = _ToolboxStub()
    broker = _broker(toolbox)
    _pending(broker, False)

    # Not broker-started: never released even while regulating.
    toolbox.running = True
    assert await broker._ensure_inner() == "idle"
    assert toolbox.stops == []

    # Broker-started owner + empty window + regulation → governed stop.
    broker._broker_started_owner = True
    assert await broker._ensure_inner() == "released"
    assert len(toolbox.stops) == 1
    assert toolbox.stops[0]["tool_id"] == OWNER_TOOL_ID
    assert broker._broker_started_owner is False

    # Not regulating → no release even for a broker-started owner.
    regulating["on"] = False
    toolbox.running = True
    broker._broker_started_owner = True
    assert await broker._ensure_inner() == "idle"
    assert len(toolbox.stops) == 1


@pytest.mark.asyncio
async def test_explicit_stop_clears_broker_ownership() -> None:
    broker = _broker(_ToolboxStub())
    broker._broker_started_owner = True
    broker.note_explicit_owner_stop()
    assert broker._broker_started_owner is False
    assert broker._explicit_stop_at > 0.0
