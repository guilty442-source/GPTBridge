"""§10.7 Ollama 按需啟動＋embedding 資源閘門測試。"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from pathlib import Path

import pytest

_SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
if str(_SRC_CORE) not in sys.path:
    sys.path.insert(0, str(_SRC_CORE))

from core_system import ollama_demand  # noqa: E402

_SHARED_SRC = Path(__file__).resolve().parents[2] / "shared-layer" / "src"
if str(_SHARED_SRC) not in sys.path:
    sys.path.insert(0, str(_SHARED_SRC))


def test_ensure_ready_short_circuits_when_reachable(monkeypatch, tmp_path):
    monkeypatch.setattr(ollama_demand, "_AUDIT_PATH", tmp_path / "audit.jsonl")
    monkeypatch.setattr(ollama_demand, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(ollama_demand, "probe_ollama", lambda: True)

    def _no_spawn():
        raise AssertionError("reachable service must not spawn")

    monkeypatch.setattr(ollama_demand, "_spawn_ollama", _no_spawn)
    assert ollama_demand.ensure_ollama_ready(timeout_s=1.0) is True


def test_ensure_ready_spawns_and_waits(monkeypatch, tmp_path):
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(ollama_demand, "_AUDIT_PATH", audit)
    monkeypatch.setattr(ollama_demand, "_STATE_PATH", tmp_path / "state.json")
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
    monkeypatch.setattr(ollama_demand, "_STATE_PATH", tmp_path / "state.json")
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
    monkeypatch.setattr(ollama_demand, "_STATE_PATH", tmp_path / "state.json")
    monkeypatch.setattr(ollama_demand.time, "sleep", lambda s: None)

    class _Reg:
        def register(self, pid, **kw):
            pass

    assert ollama_demand.ensure_ollama_ready(
        timeout_s=0.5, process_registry=_Reg()
    ) is False
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


class _OwnedReg:
    """Minimal ProcessRegistry stub honouring the is_owned contract."""

    def __init__(self, owned_pids=()):
        self._owned = set(owned_pids)
        self.shutdown_marks = []

    def is_owned(self, pid):
        return pid in self._owned

    def mark_shutdown(self, pid, state="exited"):
        self.shutdown_marks.append((pid, state))


def _wire_registry(monkeypatch, registry):
    import core_system.process_registry as pr

    monkeypatch.setattr(pr, "get_process_registry", lambda: registry)


def _wire_metrics(monkeypatch, exe="ollama.exe", ok=True, killed=None):
    import shared_layer.performance.process_metrics as pm

    def _term(pid):
        if killed is not None:
            killed.append(pid)
        return ok

    monkeypatch.setattr(pm, "process_exe", lambda pid: exe)
    monkeypatch.setattr(pm, "process_terminate", _term)


def _state_file(tmp_path, pid, last_use):
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps({"spawned_pid": pid, "last_use": last_use}),
        encoding="utf-8",
    )
    return path


def test_stop_ollama_kills_owned_idle(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ollama_demand, "_AUDIT_PATH", tmp_path / "audit.jsonl"
    )
    monkeypatch.setattr(
        ollama_demand,
        "_STATE_PATH",
        _state_file(tmp_path, 4321, time.time() - 1000),
    )
    registry = _OwnedReg(owned_pids={4321})
    _wire_registry(monkeypatch, registry)
    killed = []
    _wire_metrics(monkeypatch, killed=killed)
    assert ollama_demand.stop_ollama_if_owned(idle_s=900.0) is True
    assert killed == [4321]
    assert registry.shutdown_marks == [(4321, "exited")]
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "audit.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events == ["unload"]


def test_stop_ollama_keeps_recent_use(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ollama_demand,
        "_STATE_PATH",
        _state_file(tmp_path, 4321, time.time()),
    )
    _wire_registry(monkeypatch, _OwnedReg(owned_pids={4321}))
    killed = []
    _wire_metrics(monkeypatch, killed=killed)
    assert ollama_demand.stop_ollama_if_owned(idle_s=900.0) is False
    assert killed == []


def test_stop_ollama_never_kills_foreign(monkeypatch, tmp_path):
    """外部自行啟動的 Ollama 永不觸碰（所有權界線）。"""
    monkeypatch.setattr(
        ollama_demand,
        "_STATE_PATH",
        _state_file(tmp_path, 4321, time.time() - 10000),
    )
    _wire_registry(monkeypatch, _OwnedReg(owned_pids=set()))
    killed = []
    _wire_metrics(monkeypatch, killed=killed)
    assert ollama_demand.stop_ollama_if_owned(idle_s=0.0) is False
    assert killed == []


def test_stop_ollama_refuses_pid_reuse(monkeypatch, tmp_path):
    """pid 回收後影像名稱不符 → 拒絕終止並稽核。"""
    audit = tmp_path / "audit.jsonl"
    monkeypatch.setattr(ollama_demand, "_AUDIT_PATH", audit)
    monkeypatch.setattr(
        ollama_demand,
        "_STATE_PATH",
        _state_file(tmp_path, 4321, time.time() - 10000),
    )
    _wire_registry(monkeypatch, _OwnedReg(owned_pids={4321}))
    killed = []
    _wire_metrics(
        monkeypatch, exe="notepad.exe", killed=killed
    )
    assert ollama_demand.stop_ollama_if_owned(idle_s=0.0) is False
    assert killed == []
    assert "unload-refused" in audit.read_text(encoding="utf-8")


def _make_phase_stub():
    import startup_core.phases as phases_mod

    class _Stub(phases_mod.PhaseMixin):
        def __init__(self):
            self._stop = threading.Event()

        def _probe_tcp(self, host, port, timeout=0.75):
            return False

    return _Stub()


def test_boot_phase_never_spawns(monkeypatch):
    """§10.7：啟動階段唯讀探測——未達也絕不 spawn。"""
    monkeypatch.setattr(ollama_demand, "ollama_installed", lambda: True)

    def _no_spawn():
        raise AssertionError("boot phase must not spawn ollama")

    monkeypatch.setattr(ollama_demand, "_spawn_ollama", _no_spawn)
    result = _make_phase_stub()._phase_ollama()
    assert result["ready"] is False
    assert result["state"] == "deferred"
    assert result["on_demand"] is True
    assert result["installed"] is True


def test_boot_phase_degraded_only_when_not_installed(monkeypatch):
    monkeypatch.setattr(ollama_demand, "ollama_installed", lambda: False)
    result = _make_phase_stub()._phase_ollama()
    assert result["ready"] is False
    assert result["state"] == "degraded"
    assert result["installed"] is False


def test_ollama_installed_detection(monkeypatch, tmp_path):
    root = tmp_path / "Programs" / "Ollama"
    root.mkdir(parents=True)
    (root / "ollama.exe").write_bytes(b"x")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(ollama_demand.shutil, "which", lambda name: None)
    assert ollama_demand.ollama_installed() is True
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "empty"))
    assert ollama_demand.ollama_installed() is False
