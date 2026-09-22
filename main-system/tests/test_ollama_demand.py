"""§10.7 Ollama 按需啟動＋embedding 資源閘門測試。"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

_SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
if str(_SRC_CORE) not in sys.path:
    sys.path.insert(0, str(_SRC_CORE))

from core_system import ollama_demand  # noqa: E402


def test_ensure_ready_short_circuits_when_reachable(monkeypatch, tmp_path):
    monkeypatch.setattr(ollama_demand, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(ollama_demand, "probe_ollama", lambda: True)

    def _no_spawn():
        raise AssertionError("reachable service must not spawn")

    monkeypatch.setattr(ollama_demand, "_spawn_ollama", _no_spawn)
    assert ollama_demand.ensure_ollama_ready(timeout_s=1.0) is True


def test_ensure_ready_spawns_and_waits(monkeypatch, tmp_path):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(ollama_demand, "_AUDIT_PATH", audit)
    monkeypatch.setattr(ollama_demand, "probe_ollama", lambda: False)
    monkeypatch.setattr(
        ollama_demand, "_spawn_ollama", lambda: (4321, "ollama serve")
    )
    probes = iter([False, False, True])
    monkeypatch.setattr(
        ollama_demand, "_probe_tcp", lambda timeout=0.75: next(probes, True)
    )

    registry_calls = []

    class _Reg:
        def register(self, pid, **kw):
            registry_calls.append((pid, kw))

    assert ollama_demand.ensure_ollama_ready(
        timeout_s=5.0, process_registry=_Reg()
    ) is True
    assert registry_calls and registry_calls[0][0] == 4321
    assert registry_calls[0][1]["module_id"] == "ollama"
    events = [
        json.loads(line)["event"]
        for line in audit.read_text(encoding="utf-8").splitlines()
    ]
    assert events == ["spawn", "ready"]


def test_ensure_ready_spawn_unavailable_fails_closed(monkeypatch, tmp_path):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(ollama_demand, "_AUDIT_PATH", audit)
    monkeypatch.setattr(ollama_demand, "probe_ollama", lambda: False)
    monkeypatch.setattr(ollama_demand, "_spawn_ollama", lambda: (None, ""))
    assert ollama_demand.ensure_ollama_ready(timeout_s=1.0) is False
    assert json.loads(audit.read_text(encoding="utf-8").strip())["event"] == (
        "spawn-unavailable"
    )


def test_ensure_ready_timeout_fails_closed(monkeypatch, tmp_path):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(ollama_demand, "_AUDIT_PATH", audit)
    monkeypatch.setattr(ollama_demand, "probe_ollama", lambda: False)
    monkeypatch.setattr(
        ollama_demand, "_spawn_ollama", lambda: (4321, "ollama serve")
    )
    monkeypatch.setattr(ollama_demand, "_probe_tcp", lambda timeout=0.75: False)
    monkeypatch.setattr(ollama_demand.time, "sleep", lambda s: None)
    assert ollama_demand.ensure_ollama_ready(timeout_s=0.5) is False
    assert "timeout" in audit.read_text(encoding="utf-8")


class _Mgr:
    def __init__(self, admit: bool = True) -> None:
        self.admit = admit
        self.requests = []
        self.releases = []
        self.measurements = []

    def request_load(self, role, model_id, *, vram_mb=0, ram_mb=0):
        self.requests.append((role, model_id, ram_mb))
        return type(
            "D", (),
            {"admitted": self.admit, "reason": "deny" if not self.admit else "ok"},
        )()

    def release(self, model_id):
        self.releases.append(model_id)
        return True

    def record_measurement(self, model_id, role, metrics):
        self.measurements.append(metrics)


def _provider(monkeypatch, mgr: _Mgr):
    import core_system.model_resource_manager as mrm
    from core_system.rag.embeddings import OllamaEmbeddingProvider

    monkeypatch.setattr(mrm, "_SHARED", mgr)
    return OllamaEmbeddingProvider()


@pytest.mark.asyncio
async def test_embedding_gate_denies_fails_closed(monkeypatch):
    mgr = _Mgr(admit=False)
    provider = _provider(monkeypatch, mgr)
    with pytest.raises(RuntimeError, match="resource gate"):
        await provider._ensure_backend()
    assert mgr.requests[0][1] == "ollama/qwen3-embedding:4b"
    assert mgr.requests[0][2] == 4096
    assert provider._admitted_model_id is None


@pytest.mark.asyncio
async def test_embedding_gate_admit_and_service_up(monkeypatch):
    mgr = _Mgr(admit=True)
    provider = _provider(monkeypatch, mgr)
    monkeypatch.setattr(ollama_demand, "probe_ollama", lambda: True)

    def _no_ensure(*a, **kw):
        raise AssertionError("reachable service must not demand-start")

    monkeypatch.setattr(ollama_demand, "ensure_ollama_ready", _no_ensure)
    await provider._ensure_backend()
    assert provider._admitted_model_id == "ollama/qwen3-embedding:4b"


@pytest.mark.asyncio
async def test_embedding_demand_start_failure_releases(monkeypatch):
    mgr = _Mgr(admit=True)
    provider = _provider(monkeypatch, mgr)
    monkeypatch.setattr(ollama_demand, "probe_ollama", lambda: False)

    async def _fake_to_thread(fn, *a, **kw):
        return False

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(
        ollama_demand, "ensure_ollama_ready", lambda timeout_s: False
    )
    with pytest.raises(RuntimeError, match="demand-start"):
        await provider._ensure_backend()
    assert mgr.releases == ["ollama/qwen3-embedding:4b"]
    assert provider._admitted_model_id is None
    assert mgr.measurements[0]["event"] == "ollama_demand_start"
    assert mgr.measurements[0]["ok"] is False


@pytest.mark.asyncio
async def test_embedding_demand_start_success(monkeypatch):
    mgr = _Mgr(admit=True)
    provider = _provider(monkeypatch, mgr)
    monkeypatch.setattr(ollama_demand, "probe_ollama", lambda: False)

    async def _fake_to_thread(fn, *a, **kw):
        return fn(*a, **kw)

    monkeypatch.setattr(asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(
        ollama_demand, "ensure_ollama_ready", lambda timeout_s: True
    )
    await provider._ensure_backend()
    assert provider._admitted_model_id == "ollama/qwen3-embedding:4b"
    assert mgr.measurements[0]["ok"] is True
