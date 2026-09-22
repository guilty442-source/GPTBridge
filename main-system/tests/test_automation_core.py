"""§1.1 自動化集中（P0-4）— AutomationCore 清單／排程／審計／kill switch 測試。"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest


SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
sys.path.insert(0, str(SRC_CORE))

from tasks.automation_core import AutomationCore  # noqa: E402


class _FakeScheduler:
    """PeriodicScheduler 同形 stub：只記錄註冊，不開迴圈。"""

    def __init__(self) -> None:
        self.jobs_map: dict[str, dict] = {}

    def register(self, name, interval_s, tick, **kwargs) -> None:
        self.jobs_map[name] = {
            "interval_s": interval_s,
            "tick": tick,
            **kwargs,
        }

    def unregister(self, name) -> None:
        self.jobs_map.pop(name, None)

    def jobs(self) -> list[dict]:
        return [
            {"name": n, "last_error": None, "run_count": 0}
            for n in self.jobs_map
        ]


def _manifest(path: Path, flows: dict) -> Path:
    path.write_text(
        json.dumps({"schema": "star-automation-flows/v1", "flows": flows}),
        encoding="utf-8",
    )
    return path


def _core(tmp_path: Path, flows: dict | None = None) -> AutomationCore:
    config = tmp_path / "automation-flows.json"
    if flows is not None:
        _manifest(config, flows)
    return AutomationCore(
        _FakeScheduler(),
        config_path=config,
        state_path=tmp_path / "state.json",
        audit_ledger=tmp_path / "audit.jsonl",
    )


def _audit_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in
            path.read_text(encoding="utf-8").splitlines() if line.strip()]


_FLOW = {
    "owner": "unit-test",
    "kind": "periodic",
    "interval_s": 60,
    "pausable": True,
    "enabled": True,
}


def test_unlisted_flow_denied_and_audited(tmp_path: Path) -> None:
    core = _core(tmp_path, {"flow-a": dict(_FLOW)})

    async def tick() -> None:
        pass

    assert core.register_flow("ghost-flow", tick, interval_s=10) is False
    assert "ghost-flow" not in core._scheduler.jobs_map
    records = _audit_lines(tmp_path / "audit.jsonl")
    assert any(r["action"] == "register-denied"
               and r["flow"] == "ghost-flow" for r in records)


def test_enabled_flow_registers_through_scheduler(tmp_path: Path) -> None:
    core = _core(tmp_path, {"flow-a": dict(_FLOW)})
    ran: list[float] = []

    async def tick() -> None:
        ran.append(1.0)

    assert core.register_flow("flow-a", tick) is True
    job = core._scheduler.jobs_map["flow-a"]
    assert job["interval_s"] == 60.0
    assert job["pausable"] is True
    asyncio.run(job["tick"]())
    assert ran == [1.0]
    records = _audit_lines(tmp_path / "audit.jsonl")
    assert any(r["action"] == "registered" for r in records)
    run = [r for r in records if r["action"] == "flow-run"]
    assert run and run[-1]["outcome"] == "ok"
    assert run[-1]["owner"] == "unit-test"


def test_manifest_disabled_flow_is_kill_switched(tmp_path: Path) -> None:
    flow = dict(_FLOW)
    flow["enabled"] = False
    core = _core(tmp_path, {"flow-a": flow})

    async def tick() -> None:
        pass

    assert core.register_flow("flow-a", tick) is False
    assert "flow-a" not in core._scheduler.jobs_map
    records = _audit_lines(tmp_path / "audit.jsonl")
    assert any(r["action"] == "register-denied"
               and r["reason"] == "kill-switch" for r in records)


def test_runtime_disable_persists_and_unregisters(tmp_path: Path) -> None:
    flows = {"flow-a": dict(_FLOW)}
    core = _core(tmp_path, flows)

    async def tick() -> None:
        pass

    assert core.register_flow("flow-a", tick) is True
    assert core.disable("flow-a", reason="operator") is True
    assert "flow-a" not in core._scheduler.jobs_map
    assert core.is_enabled("flow-a") is False

    # Restart-equivalent: a fresh core sees the persisted kill.
    core2 = _core(tmp_path, flows)
    assert core2.is_enabled("flow-a") is False
    assert core2.register_flow("flow-a", tick) is False


def test_enable_restores_schedule(tmp_path: Path) -> None:
    core = _core(tmp_path, {"flow-a": dict(_FLOW)})

    async def tick() -> None:
        pass

    core.register_flow("flow-a", tick)
    core.disable("flow-a")
    assert core.enable("flow-a") is True
    assert core.is_enabled("flow-a") is True
    assert "flow-a" in core._scheduler.jobs_map


def test_corrupt_manifest_denies_everything(tmp_path: Path) -> None:
    config = tmp_path / "automation-flows.json"
    config.write_text("{not json", encoding="utf-8")
    core = AutomationCore(
        _FakeScheduler(),
        config_path=config,
        state_path=tmp_path / "state.json",
        audit_ledger=tmp_path / "audit.jsonl",
    )

    async def tick() -> None:
        pass

    assert core.register_flow("anything", tick, interval_s=5) is False
    assert core.is_enabled("anything") is False


def test_reload_manifest_kills_newly_disabled_flow(tmp_path: Path) -> None:
    flows = {"flow-a": dict(_FLOW)}
    core = _core(tmp_path, flows)

    async def tick() -> None:
        pass

    core.register_flow("flow-a", tick)
    assert "flow-a" in core._scheduler.jobs_map

    killed = dict(_FLOW)
    killed["enabled"] = False
    _manifest(tmp_path / "automation-flows.json", {"flow-a": killed})
    core.reload_manifest()
    assert "flow-a" not in core._scheduler.jobs_map
    records = _audit_lines(tmp_path / "audit.jsonl")
    assert any(r["action"] == "killed-by-manifest" for r in records)


def test_scheduler_compatible_register_alias(tmp_path: Path) -> None:
    core = _core(tmp_path, {"flow-a": dict(_FLOW)})

    async def tick() -> None:
        pass

    # PeriodicScheduler.register signature: (name, interval_s, tick, **kw)
    assert core.register("flow-a", 30.0, tick, run_immediately=True) is True
    job = core._scheduler.jobs_map["flow-a"]
    assert job["interval_s"] == 30.0
    assert job["run_immediately"] is True
    records = _audit_lines(tmp_path / "audit.jsonl")
    assert any(r["action"] == "interval-override" for r in records)


def test_flows_surface_merges_scheduler_state(tmp_path: Path) -> None:
    core = _core(tmp_path, {"flow-a": dict(_FLOW)})

    async def tick() -> None:
        pass

    core.register_flow("flow-a", tick)
    report = core.flows()
    entry = next(r for r in report if r["flow"] == "flow-a")
    assert entry["registered"] is True
    assert entry["enabled"] is True
    assert entry["owner"] == "unit-test"


@pytest.mark.asyncio
async def test_flow_run_error_audited_and_propagated(tmp_path: Path) -> None:
    core = _core(tmp_path, {"flow-a": dict(_FLOW)})

    async def tick() -> None:
        raise RuntimeError("boom")

    core.register_flow("flow-a", tick)
    job = core._scheduler.jobs_map["flow-a"]
    with pytest.raises(RuntimeError):
        await job["tick"]()
    records = _audit_lines(tmp_path / "audit.jsonl")
    run = [r for r in records if r["action"] == "flow-run"]
    assert run[-1]["outcome"] == "error"
    assert "boom" in run[-1]["error"]


def test_real_manifest_covers_registered_flows() -> None:
    """The shipped manifest must list every flow a consumer registers."""
    config = (
        Path(__file__).resolve().parents[1]
        / "config" / "automation-flows.json"
    )
    raw = json.loads(config.read_text(encoding="utf-8"))
    flows = raw["flows"]
    for flow_id in (
        "git-automation",
        "daily-global-cleaner",
        "system-automation-coordinator",
        "maintenance-controller",
        "connection-watchdog",
        "state-outbox",
        "model-service-activation",
        "resource-governor",
        "update-manager",
    ):
        assert flow_id in flows, flow_id
        assert flows[flow_id].get("owner"), flow_id
        assert flows[flow_id].get("kind") in (
            "periodic", "event", "on-demand", "private-loop")
