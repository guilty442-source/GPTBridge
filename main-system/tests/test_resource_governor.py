"""§10.64 resource governor — worker ledger, hysteresis, kill switch."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import sys
import types
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO / "scripts" / "resource-governor.py"

spec = importlib.util.spec_from_file_location("resource_governor", _SCRIPT)
gov = importlib.util.module_from_spec(spec)
sys.modules.setdefault("resource_governor", gov)
spec.loader.exec_module(gov)


class _MemInfo:
    def __init__(self, rss: int) -> None:
        self.rss = rss


class _FakeProc:
    """Minimal psutil.Process stand-in for govern_once."""

    def __init__(
        self,
        pid: int,
        name: str,
        *,
        cpu: float = 0.0,
        rss_mb: float = 10.0,
        cmdline: str = "",
        exe: str = "",
        username: str = "u",
    ) -> None:
        self._pid = pid
        self._cpu = cpu
        self._cmdline = cmdline
        self.info = {
            "pid": pid,
            "name": name,
            "exe": exe,
            "username": username,
            "memory_info": _MemInfo(int(rss_mb * 1024 * 1024)),
        }
        self.nice_calls: list[object] = []
        self.affinity_calls: list[list[int]] = []

    def oneshot(self):
        return contextlib.nullcontext(self)

    def username(self) -> str:
        return self.info["username"]

    def create_time(self) -> float:
        return 1000.0 + self._pid

    def cpu_percent(self, _interval) -> float:
        return self._cpu

    def cmdline(self) -> list[str]:
        return self._cmdline.split() if self._cmdline else []

    def nice(self, value) -> None:
        self.nice_calls.append(value)

    def cpu_affinity(self, cpus=None):
        if cpus is not None:
            self.affinity_calls.append(list(cpus))
        return list(range(8))


class _FakeMem:
    total = 64 * 1024 ** 3
    available = 40 * 1024 ** 3
    percent = 37.5


def _config(**kw) -> gov.GovernorConfig:
    args = argparse.Namespace(
        interval=5.0,
        cpu_busy=50.0,
        cpu_extreme=90.0,
        sustain=3,
        mem_trim_mb=1500.0,
        no_affinity=False,
        dry_run=True,
    )
    args.__dict__.update(kw)
    return gov.GovernorConfig(args)


def test_feature_args_preserve_explicit_disable() -> None:
    config = _config(
        probalance=False,
        cpu_limiter=True,
        background_mode=False,
        ecoqos=None,
    )
    args = gov._feature_args(config)
    assert "--no-probalance" in args
    assert "--cpu-limiter" in args
    assert "--no-background-mode" in args
    assert "--ecoqos" not in args
    assert "--no-ecoqos" not in args


def _machine() -> _FakeProc:
    return _FakeProc(9999, "python.exe", username="u")


def _run(procs, monkeypatch, regulation=None, records=None, env=None, **cfg):
    monkeypatch.setattr(
        gov.psutil, "process_iter", lambda attrs: iter(procs)
    )
    monkeypatch.setattr(
        gov.psutil, "virtual_memory", lambda: _FakeMem()
    )
    monkeypatch.setattr(gov.psutil, "cpu_percent", lambda _i: 5.0)
    monkeypatch.setattr(gov, "_self_tree", lambda: set())
    monkeypatch.setattr(gov, "_log_action", lambda payload: None)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    return gov.govern_once(
        _config(**cfg), records if records is not None else {},
        machine=_machine(), regulation=regulation,
    )


def _worker(pid: int, cpu: float) -> _FakeProc:
    return _FakeProc(
        pid, "python.exe", cpu=cpu, rss_mb=20,
        exe=r"e:\gptbridge\.venv\python.exe",
        cmdline=r"python e:\gptbridge\scripts\train.py",
    )


def test_classify_plane() -> None:
    assert (
        gov._classify_plane(_FakeProc(1, "x.exe", cmdline="chrome"), "x.exe", None)
        == "external"
    )
    gov_proc = _FakeProc(
        2, "python.exe", cmdline="python main.py --serve",
        exe=r"e:\gptbridge\main-system\.venv\python.exe",
    )
    assert gov._classify_plane(gov_proc, "python.exe", gov_proc.info["exe"]) == "governance"
    tool = _FakeProc(
        3, "python.exe", exe=r"e:\gptbridge\standalone tools\local-model\venv\python.exe"
    )
    assert gov._classify_plane(tool, "python.exe", tool.info["exe"]) == "toolbox"


def test_worker_ledger_and_hysteresis(monkeypatch) -> None:
    regulation = {"over": 0, "under": 0, "active": False}
    records: dict = {}
    procs = [_worker(101, 20.0), _worker(102, 15.0)]

    # Strict INT-10 semantics (2026-09-22 ruling): the first over-budget
    # sample engages regulation, and the pre-throttle tier engages in the
    # same sample because usage is above the 80%-of-budget band.
    snap = _run(procs, monkeypatch, regulation=regulation, records=records)
    assert regulation["active"] is True, "first over-budget sample must regulate"
    assert regulation["pre"] is True
    assert snap["worker_admission_hold"] is True
    assert snap["worker_ledger"]["over_budget"] is True

    idle = [_worker(101, 0.5), _worker(102, 0.5)]
    for _ in range(4):
        _run(idle, monkeypatch, regulation=regulation, records=records)
        assert regulation["active"] is True
        assert regulation["pre"] is True
    snap = _run(idle, monkeypatch, regulation=regulation, records=records)
    assert regulation["active"] is False, "5 under-budget samples must release"
    assert regulation["pre"] is False
    assert snap["worker_admission_hold"] is False


def test_prethrottle_middle_band(monkeypatch) -> None:
    """80%-of-budget band engages the soft pre-throttle on a single sample
    without full regulation; release needs the same 5-sample under-80%
    streak (anti-flap)."""
    regulation = {"over": 0, "under": 0, "active": False}
    records: dict = {}
    # Worker aggregate 9%: above the 80% band (8%) but below the 10% budget.
    band = [_worker(501, 5.0), _worker(502, 4.0)]

    snap = _run(band, monkeypatch, regulation=regulation, records=records)
    assert regulation["pre"] is True
    assert regulation["active"] is False, "middle band must not fully regulate"
    assert snap["worker_admission_hold"] is True

    idle = [_worker(501, 0.5), _worker(502, 0.5)]
    for _ in range(4):
        _run(idle, monkeypatch, regulation=regulation, records=records)
        assert regulation["pre"] is True
    snap = _run(idle, monkeypatch, regulation=regulation, records=records)
    assert regulation["pre"] is False
    assert snap["worker_admission_hold"] is False


def test_kill_switch_observes_only(monkeypatch) -> None:
    actions: list[dict] = []

    def _capture(payload):
        actions.append(payload)

    procs = [_worker(201, 95.0)]
    monkeypatch.setattr(gov, "_log_action", _capture)
    snap = _run(
        procs, monkeypatch, env={gov.GOVERNOR_DISABLE_ENV: "1"},
    )
    assert snap["disabled"] is True
    real = [
        a
        for a in actions
        if a.get("action") not in {"regulation-entered", "prethrottle-entered"}
    ]
    assert real == [], "kill switch must observe without acting"
    assert all(p.nice_calls == [] for p in procs)


def test_governance_plane_never_regulated(monkeypatch) -> None:
    regulation = {"over": 0, "under": 0, "active": True}
    gov_proc = _FakeProc(
        301, "python.exe", cpu=95.0,
        exe=r"e:\gptbridge\main-system\.venv\python.exe",
        cmdline="python main.py --serve",
    )
    records: dict = {}
    for _ in range(4):
        _run([gov_proc], monkeypatch, regulation=regulation, records=records)
    assert gov_proc.nice_calls == []
    assert gov_proc.affinity_calls == []


def test_worker_affinity_capped_and_restored(monkeypatch) -> None:
    """②: workers pinned to the bounded subset while regulating,
    released when regulation clears."""
    regulation = {"over": 0, "under": 0, "active": True}
    records: dict = {}
    procs = [_worker(401, 1.0)]

    _run(procs, monkeypatch, regulation=regulation, records=records,
         dry_run=False)
    worker = procs[0]
    assert worker.affinity_calls, "regulation must cap worker affinity"
    assert len(worker.affinity_calls[0]) <= max(
        1, int((os.cpu_count() or 1) * gov.WORKER_CPU_BUDGET_PCT // 100)
    )

    regulation["active"] = False
    _run(procs, monkeypatch, regulation=regulation, records=records,
         dry_run=False)
    assert worker.affinity_calls[-1] == list(range(os.cpu_count() or 1))
