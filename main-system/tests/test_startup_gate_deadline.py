"""Startup-gate deadline fail-closed proof (P0-7 residual).

When total startup-gate duration exceeds the certified deadline
(``STARTUP_GATE_DEADLINE_SECONDS``) the run must fail closed:
``gate_ok=False``, ``state=FAILED``, ``exit_code=2`` and the
StartupGate ladder must never reach CORE_READY.
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

SRC_CORE = Path(__file__).resolve().parents[1] / "src-core"
if str(SRC_CORE) not in sys.path:
    sys.path.insert(0, str(SRC_CORE))

import pytest  # noqa: E402

import startup_core.phases_execution as phases_execution  # noqa: E402
from startup_core.phases_execution import (  # noqa: E402
    StartupPhaseExecutionMixin,
)

_PHASE_NAMES = (
    "environment-check",
    "governance-audit",
    "postgresql-start",
    "qdrant-start",
    "ollama-start",
)


class _StubExecutor(StartupPhaseExecutionMixin):
    """Minimal mixin harness: real gate logic, stubbed phase probes."""

    def __init__(
        self,
        workspace_root: Path,
        *,
        slow_phase: str = "",
        delay_s: float = 0.0,
    ) -> None:
        self._stop = threading.Event()
        self.workspace_root = workspace_root
        self._PHASE_HANDLERS = {
            phase: self._handler(phase, slow_phase, delay_s)
            for phase in _PHASE_NAMES
        }

    def _ensure_runtime_paths(self) -> None:
        return None

    def _handler(self, phase: str, slow_phase: str, delay_s: float):
        def _run(_self: object) -> dict:
            if phase == slow_phase:
                time.sleep(delay_s)
            result = {
                "phase": phase,
                "ready": True,
                "duration_ms": int(delay_s * 1000) if phase == slow_phase else 0,
            }
            if phase == "postgresql-start":
                result["certification"] = {"ready": True}
            return result

        return _run


def test_deadline_exceeded_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        phases_execution, "STARTUP_GATE_DEADLINE_SECONDS", 0.05
    )
    executor = _StubExecutor(
        tmp_path, slow_phase="postgresql-start", delay_s=0.2
    )
    report = executor._run_startup_phases()
    assert report["deadline_exceeded"] is True
    assert report["gate_ok"] is False
    assert report["state"] == "FAILED"
    assert report["exit_code"] == 2
    assert report["startup_ladder"]["core_ready"] is False


def test_within_deadline_passes_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        phases_execution, "STARTUP_GATE_DEADLINE_SECONDS", 8.0
    )
    executor = _StubExecutor(tmp_path)
    report = executor._run_startup_phases()
    assert report["deadline_exceeded"] is False
    assert report["gate_ok"] is True
    assert report["state"] in ("READY", "DEGRADED")
    assert report["exit_code"] == 0


def test_stop_flag_alone_also_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        phases_execution, "STARTUP_GATE_DEADLINE_SECONDS", 8.0
    )
    executor = _StubExecutor(tmp_path)
    executor._stop.set()
    report = executor._run_startup_phases()
    assert report["gate_ok"] is False
    assert report["state"] == "FAILED"
    assert report["exit_code"] == 2
