"""P0 (blueprint section 4.8) acceptance tests.

MS1/MS2 — RAG/CAG integrations must not be imported or constructed at
composition-root time; they start on demand through
``AppLifecycleMixin.ensure_rag_cag_started`` (``GPTBRIDGE_RAG_EAGER=1``
restores the legacy eager path).

MS4 — the boot_core generation handover may only record
``global-success`` after standby readiness + health probes + the
stability window all pass; failures must write ``failed-isolated`` or
``rolled-back`` and reactivate the previous generation.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import types
from pathlib import Path

import pytest

SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"


# ---------------------------------------------------------------------------
# MS1: module-level laziness
# ---------------------------------------------------------------------------


def test_main_import_does_not_pull_rag_stack() -> None:
    """Importing the composition root must not load qdrant/rag modules."""
    code = (
        "import sys, main; "
        "print('QDRANT:' + str('qdrant_client' in sys.modules)); "
        "print('RAGRT:' + str('core_system.rag_runtime_integration' in sys.modules))"
    )
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", "-c", code],
        cwd=str(SRC_CORE),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "QDRANT:False" in proc.stdout
    assert "RAGRT:False" in proc.stdout


def test_lifecycle_defaults_to_lazy_rag_cag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GPTBRIDGE_RAG_EAGER", raising=False)
    from core_system.app_lifecycle import AppLifecycleMixin

    app = AppLifecycleMixin.__new__(AppLifecycleMixin)
    AppLifecycleMixin.__init__(app)
    assert app.rag_runtime is None
    assert app.cag_integration is None
    assert app.rag_ready is False and app.cag_ready is False


def test_eager_env_restores_legacy_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_rag = types.ModuleType("core_system.rag_runtime_integration")
    fake_cag = types.ModuleType("core_system.cag_integration")
    sentinel_runtime = object()
    sentinel_cag = object()
    fake_rag.create_rag_runtime_integration = lambda app: sentinel_runtime  # type: ignore[attr-defined]
    fake_cag.create_cag_integration = lambda app: sentinel_cag  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core_system.rag_runtime_integration", fake_rag)
    monkeypatch.setitem(sys.modules, "core_system.cag_integration", fake_cag)
    monkeypatch.setenv("GPTBRIDGE_RAG_EAGER", "1")

    from core_system.app_lifecycle import AppLifecycleMixin

    app = AppLifecycleMixin.__new__(AppLifecycleMixin)
    AppLifecycleMixin.__init__(app)
    assert app.rag_runtime is sentinel_runtime
    assert app.cag_integration is sentinel_cag


# ---------------------------------------------------------------------------
# MS2: on-demand startup entry point
# ---------------------------------------------------------------------------


class _LazyApp:
    """Minimal surface required by ensure_rag_cag_started."""

    def __init__(self) -> None:
        self.rag_runtime = None
        self.cag_integration = None
        self.rag_ready = False
        self.cag_ready = False
        self._rag_cag_start_lock = None

    from core_system.app_lifecycle import (  # noqa: F401 — bound method
        AppLifecycleMixin as _Mixin,
    )

    ensure_rag_cag_started = _Mixin.ensure_rag_cag_started


def test_ensure_rag_cag_started_builds_and_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _FakeRuntime:
        async def start(self) -> dict:
            calls.append("rag.start")
            return {"ok": True}

    class _FakeCag:
        async def start(self) -> dict:
            calls.append("cag.start")
            return {"ok": True}

    fake_rag = types.ModuleType("core_system.rag_runtime_integration")
    fake_cag = types.ModuleType("core_system.cag_integration")
    fake_rag.create_rag_runtime_integration = lambda app: _FakeRuntime()  # type: ignore[attr-defined]
    fake_cag.create_cag_integration = lambda app: _FakeCag()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core_system.rag_runtime_integration", fake_rag)
    monkeypatch.setitem(sys.modules, "core_system.cag_integration", fake_cag)

    app = _LazyApp()
    result = asyncio.run(app.ensure_rag_cag_started())
    assert result["rag"]["ok"] is True and result["cag"]["ok"] is True
    assert calls == ["rag.start", "cag.start"]  # CAG after RAG orchestrator
    assert app.rag_ready is True and app.cag_ready is True

    # Idempotent: a second call must not rebuild the integrations.
    first_runtime = app.rag_runtime
    asyncio.run(app.ensure_rag_cag_started())
    assert app.rag_runtime is first_runtime


def test_ensure_rag_cag_fails_closed_on_rag_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _BadRuntime:
        async def start(self) -> dict:
            return {"ok": False, "reason": "no-dsn"}

    class _FakeCag:
        async def start(self) -> dict:
            return {"ok": False, "reason": "no-rag-orchestrator"}

    fake_rag = types.ModuleType("core_system.rag_runtime_integration")
    fake_cag = types.ModuleType("core_system.cag_integration")
    fake_rag.create_rag_runtime_integration = lambda app: _BadRuntime()  # type: ignore[attr-defined]
    fake_cag.create_cag_integration = lambda app: _FakeCag()  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core_system.rag_runtime_integration", fake_rag)
    monkeypatch.setitem(sys.modules, "core_system.cag_integration", fake_cag)

    app = _LazyApp()
    result = asyncio.run(app.ensure_rag_cag_started())
    assert result["rag"]["ok"] is False
    assert app.rag_ready is False


# ---------------------------------------------------------------------------
# MS4: handover health gate + rollback
# ---------------------------------------------------------------------------

from boot_core_handover import BootCoreHandoverMixin  # noqa: E402


class _Gateway:
    def __init__(self) -> None:
        self.activations: list[tuple[int, str]] = []

    def activate(self, port: int, generation: str) -> None:
        self.activations.append((port, generation))

    def connection_count(self, port: int) -> int:
        return 0

    def close_generation_connections(self, port: int) -> None:
        pass


class _Standby:
    stdout = None

    def __init__(self, alive: bool = True) -> None:
        self._alive = alive

    def poll(self):
        return None if self._alive else 1


class _Handover(BootCoreHandoverMixin):
    def __init__(self, tmp_path: Path) -> None:
        self._update_request_path = tmp_path / "backend-update-request.json"
        self._last_update_operation = ""
        self._child = _Standby()
        self._gateway = _Gateway()
        self._active_backend_port = 8081
        self._active_generation = "gen-old"
        self._backend_generation_ports = [8081, 8082]
        self._stop = threading.Event()
        self._health_probe_port = 9090
        self.probe_ok = True
        self.standby_ready = True
        self.spawned: list = []

    # stubs for the pieces the mixin drives
    def _probe_health(self, port: int) -> bool:
        return self.probe_ok

    def _write_state(self, **kw) -> None:
        self.last_state = kw

    def _terminate_process(self, proc) -> None:
        pass

    def _relay(self, *_args) -> None:
        pass

    def _wait_port_available(self, port: int) -> bool:
        return True

    def _spawn_backend(self, *_args, **_kw):
        standby = _Standby()
        self.spawned.append(standby)
        return standby

    def _wait_backend_ready(self, *_args) -> bool:
        return self.standby_ready


def _request_payload(operation_id: str = "op-1", **overrides) -> dict:
    payload = {
        "schema_version": 1,
        "operation_id": operation_id,
        "certified": True,
        "artifact_hashes": {"m.py": "abc"},
        "target_generation": "gen-new",
        "terminal_status": "prepared",
    }
    payload.update(overrides)
    return payload


def test_read_update_request_rejects_terminal_and_uncertified(
    tmp_path: Path,
) -> None:
    handover = _Handover(tmp_path)
    assert handover._read_update_request() is None  # no file

    path = tmp_path / "backend-update-request.json"
    path.write_text(json.dumps(_request_payload()), encoding="utf-8")
    assert handover._read_update_request() is not None

    # A converged request must never spawn another generation.
    path.write_text(
        json.dumps(_request_payload(terminal_status="global-success")),
        encoding="utf-8",
    )
    assert handover._read_update_request() is None

    path.write_text(
        json.dumps(_request_payload(certified=False)), encoding="utf-8"
    )
    assert handover._read_update_request() is None


def test_drain_and_verify_gates_on_health(tmp_path: Path) -> None:
    handover = _Handover(tmp_path)
    standby = _Standby()
    assert handover._drain_and_verify(
        old_port=8081, new_port=8082, standby=standby, stability_window=0.1
    ) is True

    handover.probe_ok = False
    assert handover._drain_and_verify(
        old_port=8081, new_port=8082, standby=standby, stability_window=0.1
    ) is False

    handover.probe_ok = True
    assert handover._drain_and_verify(
        old_port=8081,
        new_port=8082,
        standby=_Standby(alive=False),
        stability_window=0.1,
    ) is False


def test_handover_success_marks_global_success(tmp_path: Path) -> None:
    handover = _Handover(tmp_path)
    (tmp_path / "backend-update-request.json").write_text(
        json.dumps(_request_payload()), encoding="utf-8"
    )
    ok = handover._maybe_handover(["run"], "READY")
    assert ok is True
    recorded = json.loads(
        (tmp_path / "backend-update-request.json").read_text("utf-8")
    )
    assert recorded["terminal_status"] == "global-success"
    assert recorded["active_generation"] == "gen-new"
    assert handover._gateway.activations[-1] == (8082, "gen-new")


def test_handover_unhealthy_standby_rolls_back(tmp_path: Path) -> None:
    handover = _Handover(tmp_path)
    handover.probe_ok = False  # new generation fails health gate
    (tmp_path / "backend-update-request.json").write_text(
        json.dumps(_request_payload()), encoding="utf-8"
    )
    ok = handover._maybe_handover(["run"], "READY")
    assert ok is False
    recorded = json.loads(
        (tmp_path / "backend-update-request.json").read_text("utf-8")
    )
    assert recorded["terminal_status"] == "rolled-back"
    # Gateway reactivated the previous generation; old child restored.
    assert handover._gateway.activations[-1] == (8081, "gen-old")
    assert handover._active_backend_port == 8081
    assert handover._active_generation == "gen-old"


def test_handover_standby_readiness_failure_isolated(tmp_path: Path) -> None:
    handover = _Handover(tmp_path)
    handover.standby_ready = False
    (tmp_path / "backend-update-request.json").write_text(
        json.dumps(_request_payload()), encoding="utf-8"
    )
    ok = handover._maybe_handover(["run"], "READY")
    assert ok is False
    recorded = json.loads(
        (tmp_path / "backend-update-request.json").read_text("utf-8")
    )
    assert recorded["terminal_status"] == "failed-isolated"
    assert recorded["error"] == "standby-readiness-failed"
    # Gateway never switched away from the old generation.
    assert handover._gateway.activations == []
    assert handover._active_generation == "gen-old"
