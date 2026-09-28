"""§10.64 resource governor (C++23, A608) — shipped-binary contract tests.

The control-law unit tests live in the native suite
(``native/test_suites/suite_resource_governor.cpp``, 15 deterministic
cases against a fake engine).  These tests verify the built
``native/resource_governor/bin/resource-governor.exe`` external
contract — rules modes, ``--once --dry-run`` snapshot schema,
``--status`` surface, the kill switch — plus that the backend signal
helpers in ``tasks.resource_governor_signal`` read the C++-produced
state.  All runs use an isolated ``--root`` so the live tree is never
touched.

Skipped when the binary is absent — build it with
``native/resource_governor/build.ps1`` (or the fleet
``native/test_suites/build.ps1``, which also builds it).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_EXE = (
    _REPO / "native" / "resource_governor" / "bin" / "resource-governor.exe"
)
_RULES = _REPO / "main-system" / "config" / "resource-governor-rules.json"

SNAPSHOT_KEYS = {
    "interval", "mode", "processes", "tracked", "cpu_load_pct",
    "mem_used_pct", "mem_available_mb", "resource_limits", "worker_ledger",
    "regulation", "responsiveness", "probalance", "features",
    "worker_admission_hold", "actions", "top_cpu", "top_mem", "dry_run",
    "disabled",
}


def _require_exe() -> Path:
    if sys.platform != "win32":
        pytest.skip("resource-governor.exe is Windows-only")
    if not _EXE.is_file():
        pytest.skip(
            "resource-governor.exe not built; run "
            "native/resource_governor/build.ps1 first"
        )
    return _EXE


@pytest.fixture()
def isolated_root(tmp_path: Path) -> Path:
    """A throwaway repo root carrying only the governor config."""
    config_dir = tmp_path / "main-system" / "config"
    config_dir.mkdir(parents=True)
    shutil.copy(_RULES, config_dir / "resource-governor-rules.json")
    return tmp_path


def _run(exe: Path, root: Path, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(
        [str(exe), *args, "--root", str(root)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=str(_REPO),
        env=merged,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def test_rules_modes_schema() -> None:
    """The shipped rules file carries the four governed modes."""
    rules = json.loads(_RULES.read_text(encoding="utf-8"))
    for mode in ("sleep", "low", "medium", "high"):
        assert mode in rules["modes"], f"mode {mode} missing"
    sleep = rules["modes"]["sleep"]
    assert sleep["worker_job_cap"] is True
    assert sleep["worker_cpu_budget"] == 5.0
    assert sleep["gpu_enabled"] is False
    assert rules["programs"]["cl.exe"]["exclude"] is True
    assert rules["programs"]["searchd.exe"]["exclude"] is True
    assert rules["auto_mode"] is True
    schedule = rules["power_saving_schedule"]
    assert schedule["enabled"] is True
    assert schedule["start"] == "22:00"
    assert schedule["mode"] == "sleep"


def test_status_surface(isolated_root: Path) -> None:
    exe = _require_exe()
    proc = _run(exe, isolated_root, "--status")
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["running"] is False
    assert str(payload["lock_file"]).endswith("resource-governor.lock")
    assert isinstance(payload.get("recent_actions"), list)


def test_once_dry_run_contract(isolated_root: Path) -> None:
    """``--once --dry-run`` emits the full snapshot schema Python backends read."""
    exe = _require_exe()
    proc = _run(exe, isolated_root, "--once", "--dry-run", "--interval", "1")
    assert proc.returncode == 0, proc.stderr
    snapshot = json.loads(proc.stdout)
    missing = SNAPSHOT_KEYS - set(snapshot)
    assert not missing, f"snapshot keys missing: {missing}"
    rules = json.loads(_RULES.read_text(encoding="utf-8"))
    assert snapshot["mode"] in rules["modes"]
    assert snapshot["dry_run"] is True
    assert snapshot["disabled"] is False
    ledger = snapshot["worker_ledger"]
    for key in ("cpu_pct", "ram_mb", "ram_pct", "budget_cpu_pct",
                "budget_ram_pct", "over_budget", "planes"):
        assert key in ledger, f"ledger key missing: {key}"
    regulation = snapshot["regulation"]
    for key in ("active", "pre", "over_samples", "under_samples", "strained"):
        assert key in regulation, f"regulation key missing: {key}"
    assert isinstance(snapshot["worker_admission_hold"], bool)
    assert isinstance(snapshot["actions"], list)
    assert isinstance(snapshot["top_cpu"], list)
    assert isinstance(snapshot["top_mem"], list)
    assert snapshot["features"]["rules_error"] is None
    assert snapshot["features"]["rules_path"].endswith(
        "resource-governor-rules.json"
    )

    state_file = (
        isolated_root / "main-system" / "runtime" / "state"
        / "resource-governor.json"
    )
    assert state_file.is_file()
    stored = json.loads(state_file.read_text(encoding="utf-8"))
    assert SNAPSHOT_KEYS - set(stored) == set()
    log_file = (
        isolated_root / "main-system" / "runtime" / "logs"
        / "resource-governor.jsonl"
    )
    assert log_file.is_file()


def test_kill_switch_observes_only(isolated_root: Path) -> None:
    """GPTBRIDGE_GOVERNOR_DISABLE=1: monitoring continues, no actions."""
    exe = _require_exe()
    proc = _run(
        exe, isolated_root, "--once", "--dry-run", "--interval", "1",
        env={"GPTBRIDGE_GOVERNOR_DISABLE": "1"},
    )
    assert proc.returncode == 0, proc.stderr
    snapshot = json.loads(proc.stdout)
    assert snapshot["disabled"] is True
    assert snapshot["actions"] == []


def test_backend_signals_read_cpp_state(
    isolated_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Backend signal helpers stay compatible with C++-produced state."""
    exe = _require_exe()
    proc = _run(exe, isolated_root, "--once", "--dry-run", "--interval", "1")
    assert proc.returncode == 0, proc.stderr
    emitted_mode = json.loads(proc.stdout)["mode"]

    from tasks import resource_governor_signal as sig

    state_file = (
        isolated_root / "main-system" / "runtime" / "state"
        / "resource-governor.json"
    )
    monkeypatch.setattr(sig, "_STATE_FILE", state_file)
    monkeypatch.setattr(
        sig, "_RULES_FILE",
        isolated_root / "main-system" / "config"
        / "resource-governor-rules.json",
    )
    mode = sig.governor_mode()
    assert mode["mode"] == emitted_mode
    assert mode["applied"] == emitted_mode
    assert mode["running"] is True
    assert mode["auto_mode"] is True
    assert isinstance(sig.regulation_active(), bool)
    assert isinstance(sig.worker_admission_hold(), bool)
