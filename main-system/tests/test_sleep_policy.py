"""§10.66 sleep policy manager tests."""
from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "main-system" / "src-core"))
sys.path.insert(0, str(ROOT / "shared-layer" / "src"))

from tasks.sleep_policy import SleepPolicyManager  # noqa: E402


class _FakeToolbox:
    def __init__(self, running: list[str]) -> None:
        self._active_request_by_tool = {t: f"req-{t}" for t in running}
        self._started_request_by_tool: dict[str, str] = {}
        self.stopped: list[str] = []
        self.stop_ok = True

    async def stop_tool(self, payload):
        if self.stop_ok:
            self.stopped.append(payload["tool_id"])
        return {"ok": self.stop_ok}


class _App:
    _shutting_down = False


def _policy(**overrides):
    base = {
        "enabled": True,
        "scan_interval_s": 60.0,
        "warm_after_s": 100.0,
        "cold_after_s": 200.0,
        "never_sleep": ["main-system"],
        "units": {},
    }
    base.update(overrides)
    return base


def _run(mgr, toolbox, policy, drained_map):
    async def drain(tool_id):
        return drained_map.get(tool_id, (True, time.time() - 1000))

    mgr._drain_state = drain  # type: ignore[attr-defined]
    asyncio.run(mgr._scan(policy))


def test_disabled_policy_no_transitions():
    toolbox = _FakeToolbox(["file-sorter"])
    mgr = SleepPolicyManager(_App(), toolbox)
    asyncio.run(mgr._scan(_policy(enabled=False)))
    assert toolbox.stopped == []
    assert mgr._tiers == {}


def test_never_sleep_exempt():
    toolbox = _FakeToolbox(["main-system"])
    mgr = SleepPolicyManager(_App(), toolbox)
    _run(mgr, toolbox, _policy(), {"main-system": (True, 0)})
    assert mgr._tiers["main-system"] == "hot"
    assert mgr._last_decisions["main-system"] == "exempt"
    assert toolbox.stopped == []


def test_in_flight_stays_hot():
    toolbox = _FakeToolbox(["file-sorter"])
    mgr = SleepPolicyManager(_App(), toolbox)
    _run(mgr, toolbox, _policy(), {"file-sorter": (False, time.time())})
    assert mgr._tiers["file-sorter"] == "hot"
    assert mgr._last_decisions["file-sorter"] == "in-flight"
    assert toolbox.stopped == []


def test_idle_tiers_warm_then_cold():
    toolbox = _FakeToolbox(["file-sorter"])
    mgr = SleepPolicyManager(_App(), toolbox)
    past = time.time() - 150  # between warm and cold thresholds
    _run(mgr, toolbox, _policy(), {"file-sorter": (True, past)})
    assert mgr._tiers["file-sorter"] == "warm"
    assert toolbox.stopped == []

    # idle marker crosses the cold threshold (no new activity observed)
    mgr._last_active["file-sorter"] = time.time() - 250
    _run(mgr, toolbox, _policy(), {"file-sorter": (True, None)})
    assert mgr._tiers["file-sorter"] == "cold"
    assert toolbox.stopped == ["file-sorter"]


def test_cold_sleep_drain_abort():
    """A request arriving mid-scan aborts the cold sleep (no lost work)."""
    toolbox = _FakeToolbox(["file-sorter"])
    mgr = SleepPolicyManager(_App(), toolbox)
    calls = {"n": 0}

    async def drain(tool_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return True, time.time() - 999  # idle long enough for cold
        return False, time.time()  # re-check sees in-flight

    mgr._drain_state = drain  # type: ignore[attr-defined]
    asyncio.run(mgr._scan(_policy()))
    assert mgr._tiers["file-sorter"] == "hot"
    assert mgr._last_decisions["file-sorter"] == "drain-abort"
    assert toolbox.stopped == []


def test_failed_stop_keeps_warm():
    toolbox = _FakeToolbox(["file-sorter"])
    toolbox.stop_ok = False
    mgr = SleepPolicyManager(_App(), toolbox)
    _run(mgr, toolbox, _policy(), {"file-sorter": (True, time.time() - 999)})
    assert mgr._last_decisions["file-sorter"] == "cold-sleep-failed"
    assert toolbox.stopped == []


def test_unit_override_thresholds():
    toolbox = _FakeToolbox(["file-sorter"])
    mgr = SleepPolicyManager(_App(), toolbox)
    policy = _policy(units={"file-sorter": {"cold_after_s": 10}})
    _run(mgr, toolbox, policy, {"file-sorter": (True, time.time() - 50)})
    assert mgr._tiers["file-sorter"] == "cold"
    assert toolbox.stopped == ["file-sorter"]
