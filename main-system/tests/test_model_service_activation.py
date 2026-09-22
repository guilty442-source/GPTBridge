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


class _Admit:
    def __init__(self, admitted: bool, reason: str) -> None:
        self.admitted = admitted
        self.reason = reason


class _ResourceManagerStub:
    def __init__(self, *, admit: bool = True) -> None:
        self.admit = admit
        self.requests: list[tuple] = []
        self.released: list[str] = []
        self.measurements: list[tuple] = []

    def request_load(self, role, model_id, *, vram_mb=0, ram_mb=0):
        self.requests.append((role, model_id, ram_mb))
        return _Admit(self.admit, "admitted" if self.admit else "ram-insufficient")

    def release(self, model_id):
        self.released.append(model_id)
        return True

    def record_measurement(self, model_id, role, metrics):
        self.measurements.append((model_id, metrics))


def _project_with_weights(tmp_path: Path, size_bytes: int = 1024 * 1024) -> Path:
    tool_root = tmp_path / "Standalone tools" / "local-model"
    settings = tool_root / "runtime" / "settings"
    weights = tool_root / "weights" / "final.pt"
    settings.mkdir(parents=True)
    weights.parent.mkdir(parents=True)
    weights.write_bytes(b"x" * size_bytes)
    settings.joinpath("native-engine.json").write_text(
        '{"checkpoint": "weights/final.pt"}', encoding="utf-8"
    )
    return tmp_path


def _gated_broker(
    toolbox: _ToolboxStub, mgr: _ResourceManagerStub, project_root: Path
) -> ModelServiceActivationBroker:
    return ModelServiceActivationBroker(
        app=object(),
        toolbox_service=toolbox,
        cooldown=0.01,
        project_root=project_root,
        resource_manager=mgr,
    )


@pytest.mark.asyncio
async def test_resource_gate_denies_and_backs_off(tmp_path) -> None:
    """§10.7: 資源閘門否決 → fail-closed 不啟動、退避、記帳。"""
    mgr = _ResourceManagerStub(admit=False)
    toolbox = _ToolboxStub()
    broker = _gated_broker(toolbox, mgr, _project_with_weights(tmp_path))
    _pending(broker, True)
    decision = await broker._ensure_inner()
    assert decision == "resource-denied"
    assert toolbox.starts == []
    assert len(mgr.requests) == 1
    role, model_id, ram_mb = mgr.requests[0]
    assert model_id == "xingcheng"
    assert getattr(role, "value", role) == "xingcheng_native"
    assert ram_mb >= int(1.0 * 1.5) + 1  # 1MB 權重 ×1.5 headroom
    assert mgr.measurements and mgr.measurements[0][1]["admitted"] is False
    assert broker._backoff > broker.min_backoff


@pytest.mark.asyncio
async def test_resource_gate_admits_then_start(tmp_path) -> None:
    mgr = _ResourceManagerStub(admit=True)
    toolbox = _ToolboxStub()
    broker = _gated_broker(toolbox, mgr, _project_with_weights(tmp_path))
    _pending(broker, True)
    decision = await broker._ensure_inner()
    assert decision == "started"
    assert len(toolbox.starts) == 1
    assert mgr.released == []  # 啟動成功保留 admission


@pytest.mark.asyncio
async def test_failed_start_releases_admission(tmp_path) -> None:
    class _FailingToolbox(_ToolboxStub):
        async def start_tool(self, payload: dict) -> dict:
            return {"ok": False, "message": "boom"}

    mgr = _ResourceManagerStub(admit=True)
    toolbox = _FailingToolbox()
    broker = _gated_broker(toolbox, mgr, _project_with_weights(tmp_path))
    _pending(broker, True)
    decision = await broker._ensure_inner()
    assert decision == "start-failed"
    assert mgr.released == ["xingcheng"]


@pytest.mark.asyncio
async def test_estimate_missing_fails_closed(tmp_path) -> None:
    """找不到釘定權重 → fail-closed，不進 request_load。"""
    mgr = _ResourceManagerStub(admit=True)
    toolbox = _ToolboxStub()
    broker = _gated_broker(toolbox, mgr, tmp_path)  # 無 weights tree
    _pending(broker, True)
    decision = await broker._ensure_inner()
    assert decision == "resource-estimate-unavailable"
    assert toolbox.starts == []
    assert mgr.requests == []


@pytest.mark.asyncio
async def test_governed_release_frees_admission(tmp_path, monkeypatch) -> None:
    import tasks.model_service_activation as mod

    monkeypatch.setattr(mod, "regulation_active", lambda: True)
    mgr = _ResourceManagerStub()
    toolbox = _ToolboxStub(running=True)
    broker = _gated_broker(toolbox, mgr, _project_with_weights(tmp_path))
    _pending(broker, False)
    broker._broker_started_owner = True
    decision = await broker._ensure_inner()
    assert decision == "released"
    assert mgr.released == ["xingcheng"]


def test_explicit_stop_frees_admission(tmp_path) -> None:
    mgr = _ResourceManagerStub()
    broker = _gated_broker(
        _ToolboxStub(), mgr, _project_with_weights(tmp_path)
    )
    broker.note_explicit_owner_stop()
    assert mgr.released == ["xingcheng"]
