"""§10.64 resource governor — worker ledger, hysteresis, kill switch,
plus the Process Lasso-inspired tier (rules, ProBalance, CPU limiter,
background mode, EcoQoS, monitoring surface)."""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
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
    """Minimal _Proc stand-in for govern_once."""

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


def _run(
    procs,
    monkeypatch,
    regulation=None,
    records=None,
    env=None,
    resp_latency=None,
    **cfg,
):
    monkeypatch.setattr(gov, "_iter_procs", lambda: iter(procs))
    monkeypatch.setattr(gov, "_VirtualMemory", lambda: _FakeMem())
    monkeypatch.setattr(gov._pm, "cpu_percent", lambda *a, **k: 5.0)
    monkeypatch.setattr(gov, "_self_tree", lambda: set())
    monkeypatch.setattr(gov, "_log_action", lambda payload: None)
    monkeypatch.setattr(gov, "_write_state", lambda payload: None)
    if resp_latency is not None:
        monkeypatch.setattr(
            gov, "measure_responsiveness", lambda runs=3: resp_latency
        )
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
    # P8 unit fix: proc.cpu_percent is per-core scale, the budget is
    # machine-%; pin an 8-logical host so per-core sums map deterministically
    # (sum 90 -> 11.25% machine, over the 10% budget).
    monkeypatch.setattr(gov.os, "cpu_count", lambda: 8)
    regulation = {"over": 0, "under": 0, "active": False}
    records: dict = {}
    procs = [_worker(101, 50.0), _worker(102, 40.0)]

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
    band = [_worker(501, 40.0), _worker(502, 32.0)]

    monkeypatch.setattr(gov.os, "cpu_count", lambda: 8)
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


# ---------------------------------------------------------------------------
# Process Lasso-inspired tier
# ---------------------------------------------------------------------------
def _patch_lasso_actions(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    def bg(pid, enable):
        calls.append(("bg", pid, enable))
        return True

    def eco(pid, enable):
        calls.append(("eco", pid, enable))
        return True

    def limit(key, pid, percent, **_kwargs):
        calls.append(("limit", pid, percent))
        return True

    def clear(key):
        calls.append(("clear", key[0], None))
        return True

    monkeypatch.setattr(gov, "_set_background_mode", bg)
    monkeypatch.setattr(gov, "_set_ecoqos", eco)
    monkeypatch.setattr(gov, "_set_cpu_limit", limit)
    monkeypatch.setattr(gov, "_clear_cpu_limit", clear)
    return calls


def test_rules_loading_and_validation(tmp_path: Path) -> None:
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "defaults": {"probalance": True, "limiter_percent": 25},
                "programs": {
                    "Train.EXE": {
                        "priority": "below_normal",
                        "affinity": [0, 1],
                        "cpu_limit_percent": 30,
                        "background": True,
                        "ecoqos": True,
                    },
                    "chrome.exe": {"exclude": True},
                },
            }
        ),
        encoding="utf-8",
    )
    defaults, programs, error, _mode = gov.load_rules(rules_path)
    assert error is None
    assert defaults["probalance"] is True
    assert programs["train.exe"].priority == gov._pm.PRIORITY_BELOW_NORMAL
    assert programs["train.exe"].affinity == [0, 1]
    assert programs["train.exe"].cpu_limit_percent == 30.0
    assert programs["train.exe"].background is True
    assert programs["chrome.exe"].exclude is True

    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    defaults, programs, error, _mode = gov.load_rules(bad)
    assert defaults == {} and programs == {} and error

    assert gov.load_rules(tmp_path / "missing.json") == ({}, {}, None, None)
    assert gov._cpu_rate_value(0.1) == 10
    assert gov._cpu_rate_value(150.0) == 10000


def test_feature_flags_default_off(tmp_path: Path) -> None:
    config = _config(rules=str(tmp_path / "none.json"))
    features = gov._resolve_features(config, {})
    assert features["probalance"] is False
    assert features["cpu_limiter"] is False
    assert features["background_mode"] is False
    assert features["ecoqos"] is False
    assert features["limiter_percent"] == gov.DEFAULT_LIMITER_PERCENT
    assert features["resp_ratio"] == gov.RESP_STRAIN_RATIO

    rules_off = _config(probalance=False)
    assert gov._resolve_features(rules_off, {"probalance": True})["probalance"] is False

    enabled = _config(probalance=True, ecoqos=True, limiter_percent=33.0)
    features = gov._resolve_features(enabled, {})
    assert features["probalance"] is True
    assert features["ecoqos"] is True
    assert features["limiter_percent"] == 33.0


def test_rule_actions_applied_once_and_held(
    tmp_path: Path, monkeypatch
) -> None:
    rules_path = tmp_path / "rules.json"
    rules_path.write_text(
        json.dumps(
            {
                "programs": {
                    "python.exe": {
                        "priority": "below_normal",
                        "affinity": [0],
                        "cpu_limit_percent": 30,
                        "background": True,
                        "ecoqos": True,
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    calls = _patch_lasso_actions(monkeypatch)
    procs = [_worker(601, 1.0)]
    records: dict = {}
    cfg = dict(
        rules=str(rules_path), background_mode=True, ecoqos=True,
        cpu_limiter=True, dry_run=False,
    )
    _run(procs, monkeypatch, records=records, resp_latency=50.0, **cfg)
    assert [c for c in calls if c[0] == "bg"] == [("bg", 601, True)]
    assert [c for c in calls if c[0] == "eco"] == [("eco", 601, True)]
    assert [c for c in calls if c[0] == "limit"] == [("limit", 601, 30.0)]
    assert procs[0].affinity_calls[-1] == [0]
    # background rule suppresses the explicit rule priority (idle CPU wins)
    assert gov._pm.PRIORITY_BELOW_NORMAL not in procs[0].nice_calls

    _run(procs, monkeypatch, records=records, resp_latency=50.0, **cfg)
    assert len([c for c in calls if c[0] == "bg"]) == 1, "rule applies once"
    record = next(iter(records.values()))
    assert {"bg", "eco", "limit", "affinity"} <= record.rule_hold
    assert record.rule_applied is True


def test_dynamic_lasso_tiers_feature_gated(monkeypatch) -> None:
    calls = _patch_lasso_actions(monkeypatch)
    monkeypatch.setattr(gov, "_foreground_pid", lambda: None)
    procs = [_worker(701, 95.0)]
    records: dict = {}
    regulation = {"over": 0, "under": 0, "active": False, "pre": False}
    for _ in range(6):
        _run(procs, monkeypatch, regulation=regulation, records=records,
             resp_latency=50.0, dry_run=False, worker_job_cap=False,
             rules="nonexistent/resource-governor-rules.json")
    assert calls == [], "new tiers must stay off until enabled"

    records = {}
    regulation = {"over": 0, "under": 0, "active": False, "pre": False}
    cfg = dict(
        background_mode=True, ecoqos=True, cpu_limiter=True,
        limiter_percent=5.0, dry_run=False,
    )
    for _ in range(6):
        _run(procs, monkeypatch, regulation=regulation, records=records,
             resp_latency=50.0, **cfg)
    assert ("bg", 701, True) in calls
    assert ("eco", 701, True) in calls
    assert any(c[0] == "limit" and c[2] == 5.0 for c in calls)

    release_calls = [c for c in calls if c[0] in {"bg", "eco"} and c[2] is False]
    assert release_calls == []
    calm = [_worker(701, 1.0)]
    for _ in range(gov.CALM_SAMPLES + 1):
        _run(calm, monkeypatch, regulation=regulation, records=records,
             resp_latency=50.0, **cfg)
    assert ("bg", 701, False) in calls
    assert ("eco", 701, False) in calls
    assert any(c[0] == "clear" for c in calls)


def test_probalance_demotes_and_restores(monkeypatch) -> None:
    monkeypatch.setattr(gov, "_foreground_pid", lambda: None)
    monkeypatch.setattr(gov, "measure_responsiveness", lambda runs=3: 220.0)
    cfg = dict(probalance=True, dry_run=False)
    # External plane: not subject to the worker-plane quick priority path,
    # so ProBalance is the tier that acts (as in Process Lasso).
    procs = [_FakeProc(801, "app.exe", cpu=60.0, exe=r"c:\apps\app.exe")]
    records: dict = {}
    regulation = {
        "over": 0, "under": 0, "active": False, "pre": False,
        "resp_baseline": 100.0,
    }
    _run(procs, monkeypatch, regulation=regulation, records=records, **cfg)
    assert procs[0].nice_calls == [], "one strain sample must not demote"

    _run(procs, monkeypatch, regulation=regulation, records=records, **cfg)
    record = next(iter(records.values()))
    assert regulation["strained"] is True
    assert record.pb_set is True
    assert gov._pm.PRIORITY_BELOW_NORMAL in procs[0].nice_calls

    monkeypatch.setattr(gov, "measure_responsiveness", lambda runs=3: 100.0)
    calm = [_FakeProc(801, "app.exe", cpu=1.0, exe=r"c:\apps\app.exe")]
    for _ in range(gov.RESP_CALM_SAMPLES + 1):
        _run(calm, monkeypatch, regulation=regulation, records=records, **cfg)
    assert regulation["strained"] is False
    assert record.pb_set is False


def test_monitoring_surface_fields(monkeypatch) -> None:
    monkeypatch.setattr(gov, "_foreground_pid", lambda: None)
    proc = _worker(901, 1.0)
    proc.info["io_counters"] = types.SimpleNamespace(
        read_bytes=5 * 1024 ** 2, write_bytes=2 * 1024 ** 2
    )
    snap = _run([proc], monkeypatch, resp_latency=42.0,
                rules="nonexistent/resource-governor-rules.json")
    row = snap["top_cpu"][0]
    assert row["io_read_mb"] == 5.0
    assert row["io_write_mb"] == 2.0
    assert row["flags"] == []
    assert snap["responsiveness"]["latency_ms"] == 42.0
    assert snap["responsiveness"]["strained"] is False
    assert snap["probalance"]["enabled"] is False
    assert snap["features"]["probalance"] is False
    assert snap["features"]["cpu_limiter"] is False
    assert snap["features"]["rules_error"] is None
    assert snap["features"]["rules_path"].endswith("resource-governor-rules.json")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only APIs")
@pytest.mark.skipif(
    os.environ.get("GPTBRIDGE_GOVERNOR_API_PROBE") != "1",
    reason="set GPTBRIDGE_GOVERNOR_API_PROBE=1 to run the live API probe",
)
def test_windows_api_probe_on_child_process() -> None:
    """Live probe: background mode / EcoQoS / Job Object CPU cap round-trip
    against a real child process (evidence; not part of the default gate)."""
    import subprocess
    import time as _time

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"]
    )
    key = (child.pid, 0.0)
    try:
        _time.sleep(1.0)
        assert gov._set_background_mode(child.pid, True) is True
        assert gov._set_ecoqos(child.pid, True) is True
        assert gov._set_cpu_limit(key, child.pid, 5.0) is True
        assert gov._set_cpu_limit(key, child.pid, 2.0) is True
        assert gov._clear_cpu_limit(key) is True
        assert gov._set_ecoqos(child.pid, False) is True
        assert gov._set_background_mode(child.pid, False) is True
    finally:
        child.terminate()
        child.wait(timeout=10)


# ---------------------------------------------------------------------------
# Worker-plane aggregate Job Object hard cap (INT-10 strict budget)
# ---------------------------------------------------------------------------
def test_worker_job_cap_assigns_shared_aggregate_job(monkeypatch) -> None:
    """Every worker-plane process joins the shared job at the aggregate
    budget rate; children spawned by members inherit the cap at birth."""
    calls = _patch_lasso_actions(monkeypatch)
    procs = [_worker(810, 5.0), _worker(811, 5.0)]
    records: dict = {}
    regulation = {"over": 0, "under": 0, "active": False, "pre": False}
    cfg = dict(worker_job_cap=True, dry_run=False)
    _run(procs, monkeypatch, regulation=regulation, records=records, **cfg)
    limited = [c for c in calls if c[0] == "limit"]
    assert limited == [
        ("limit", 810, gov.WORKER_CPU_BUDGET_PCT),
        ("limit", 811, gov.WORKER_CPU_BUDGET_PCT),
    ]
    _run(procs, monkeypatch, regulation=regulation, records=records, **cfg)
    assert (
        len([c for c in calls if c[0] == "limit"]) == 2
    ), "job membership is assigned once per process"


def test_worker_job_cap_defaults_off(monkeypatch) -> None:
    # P7: worker_job_cap is now a governed default (rules JSON defaults on);
    # this test asserts the explicit opt-out still wins over the default.
    calls = _patch_lasso_actions(monkeypatch)
    procs = [_worker(820, 95.0)]
    _run(procs, monkeypatch, worker_job_cap=False, dry_run=False)
    assert [c for c in calls if c[0] == "limit"] == []


def test_worker_job_cap_percent_override(monkeypatch) -> None:
    calls = _patch_lasso_actions(monkeypatch)
    procs = [_worker(830, 5.0)]
    _run(
        procs, monkeypatch,
        worker_job_cap=True, worker_job_percent=7.5, dry_run=False,
    )
    assert ("limit", 830, 7.5) in calls


def test_worker_job_cap_governance_plane_exempt(monkeypatch) -> None:
    calls = _patch_lasso_actions(monkeypatch)
    gov_proc = _FakeProc(
        840, "python.exe", cpu=5.0,
        exe=r"e:\gptbridge\main-system\.venv\python.exe",
        cmdline="python main.py --serve",
    )
    _run([gov_proc], monkeypatch, worker_job_cap=True, dry_run=False)
    assert calls == [], "governance plane must never join the worker job"


def test_worker_job_cap_feature_args_preserved() -> None:
    config = _config(worker_job_cap=True, worker_job_percent=7.5)
    args = gov._feature_args(config)
    assert "--worker-job-cap" in args
    assert "--worker-job-percent" in args
    assert "7.5" in args
