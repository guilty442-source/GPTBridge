"""CAG context-refresh loop — §1.1 automation-core governance.

The refresh cadence must be a governed flow when an automation core is
present: denied registration (kill switch / unlisted) must NOT fall back
to a private loop — queries degrade to RAG, the existing fail-soft path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src-core"))

from core_system import cag_integration as ci  # noqa: E402


class _FakeCore:
    def __init__(self, allow: bool = True) -> None:
        self.allow = allow
        self.registered: dict[str, tuple] = {}
        self.unregistered: list[str] = []

    def register_flow(self, flow_id, tick, **kwargs):
        if not self.allow:
            return False
        self.registered[flow_id] = (tick, kwargs)
        return True

    def unregister(self, name):
        self.unregistered.append(name)


class _FakeApp:
    def __init__(self, core=None) -> None:
        self.rag_orchestrator = object()
        self.automation_core = core
        self.logs = []

    def _log(self, data):
        self.logs.append(data)


class _FakeManager:
    def stats(self):
        return {"contexts": 0}

    def seconds_until_expiry(self, modules):
        return None


def _stub_hybrid():
    return SimpleNamespace(
        cag_loader=object(),
        cag_manager=_FakeManager(),
        cag_router=object(),
    )


def _patched(monkeypatch):
    monkeypatch.setattr(ci, "create_hybrid_orchestrator", lambda *a, **k: _stub_hybrid())
    monkeypatch.setattr(
        ci.CAGIntegration, "_build_retrievers", lambda self: {}
    )


@pytest.mark.asyncio
async def test_refresh_registers_with_automation_core(monkeypatch):
    _patched(monkeypatch)
    app = _FakeApp(core=_FakeCore())
    integration = ci.CAGIntegration(app=app)
    result = await integration.start()
    assert result["ok"] is True
    assert integration._refresh_core_driven is True
    assert integration._refresh_task is None  # no private loop
    assert "cag-context-refresh" in app.automation_core.registered
    tick, kwargs = app.automation_core.registered["cag-context-refresh"]
    assert kwargs.get("pausable") is True
    await tick()  # single pass runs without raising
    await integration.stop()
    assert "cag-context-refresh" in app.automation_core.unregistered


@pytest.mark.asyncio
async def test_refresh_denied_no_private_fallback(monkeypatch):
    _patched(monkeypatch)
    app = _FakeApp(core=_FakeCore(allow=False))
    integration = ci.CAGIntegration(app=app)
    result = await integration.start()
    assert result["ok"] is True
    assert integration._refresh_core_driven is False
    assert integration._refresh_task is None  # kill switch: no private loop
    await integration.stop()


@pytest.mark.asyncio
async def test_refresh_private_fallback_without_core(monkeypatch):
    _patched(monkeypatch)
    app = _FakeApp(core=None)
    integration = ci.CAGIntegration(app=app)
    result = await integration.start()
    assert result["ok"] is True
    assert integration._refresh_core_driven is None
    assert integration._refresh_task is not None
    await integration.stop()
    assert integration._refresh_task is None
