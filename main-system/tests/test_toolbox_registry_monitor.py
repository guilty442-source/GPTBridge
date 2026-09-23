"""ToolboxService process-registry monitor — governed flow migration (P5)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from tasks.toolbox_service import ToolboxService


class _FakeCore:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.registered: dict[str, dict] = {}
        self.unregistered: list[str] = []

    def register_flow(self, flow_id, tick, **kwargs):
        if not self.allow:
            return False
        self.registered[flow_id] = {"tick": tick, **kwargs}
        return True

    def unregister(self, name):
        self.unregistered.append(name)


def _service(tmp_path):
    svc = ToolboxService(tmp_path)
    svc.reconcile_process_registry = lambda: {"reconciled": 0}
    return svc


def test_registry_monitor_registers_governed_flow(tmp_path):
    svc = _service(tmp_path)
    core = _FakeCore()
    asyncio.run(
        svc.start_process_registry_monitor(automation_core=core)
    )
    assert svc._registry_reconcile_core is True
    assert svc._registry_reconcile_task is None
    entry = core.registered["toolbox-registry-reconcile"]
    assert entry["interval_s"] == 30.0


def test_registry_monitor_denied_no_private_fallback(tmp_path):
    svc = _service(tmp_path)
    core = _FakeCore(allow=False)
    asyncio.run(
        svc.start_process_registry_monitor(automation_core=core)
    )
    assert svc._registry_reconcile_core is False
    assert svc._registry_reconcile_task is None


def test_registry_monitor_unregister_on_stop(tmp_path):
    svc = _service(tmp_path)
    core = _FakeCore()
    asyncio.run(
        svc.start_process_registry_monitor(automation_core=core)
    )
    asyncio.run(
        svc.stop_process_registry_monitor(automation_core=core)
    )
    assert "toolbox-registry-reconcile" in core.unregistered
    assert svc._registry_reconcile_core is False


def test_registry_monitor_private_fallback_without_core(tmp_path):
    async def run() -> ToolboxService:
        svc = _service(tmp_path)
        await svc.start_process_registry_monitor()
        return svc

    svc = asyncio.run(run())
    assert svc._registry_reconcile_task is not None
    assert svc._registry_reconcile_core is False
    asyncio.run(svc.stop_process_registry_monitor())
    assert svc._registry_reconcile_task is None
