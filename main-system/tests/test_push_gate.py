"""Push gate tests (§10.69 C/F): native-suite gate + evidence records.

G97: the gate consumes the C# ``TestSuiteOrchestrator`` (sole native
orchestrator) — tests inject an orchestrator-level seam that writes the
typed ``native-orchestration-report.json`` the gate consumes.
"""
from __future__ import annotations

import json
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from governance_rule.execution.git_tiers import push_gate
from governance_rule.execution.git_tiers.process_lock import LockBusyError


@contextmanager
def _null_lock():
    yield


def _completed(returncode: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["orch"], returncode=returncode,
                                       stdout=out, stderr="")


def _bin_dir(root: Path) -> Path:
    d = root / "native" / "test_suites" / "bin"
    d.mkdir(parents=True, exist_ok=True)
    _manifest(d)
    return d


def _manifest(bin_dir: Path, revision: str = "testrev") -> Path:
    p = bin_dir / "suite-manifest.json"
    p.write_text(json.dumps({
        "schema": "native-suite-manifest/v1",
        "revision": revision,
        "built_at": "2026-01-01T00:00:00Z",
        "suites": [{"name": "alpha_suite", "exe": "alpha_suite.exe"}],
    }), encoding="utf-8")
    return p


def _exe(bin_dir: Path, name: str = "alpha_suite.exe") -> Path:
    p = bin_dir / name
    p.write_bytes(b"MZ")
    return p


def _orch_report(passed=1, failed=0, blocked=0, cases=1, verdict="PASS",
                 suites=None, failures=None, blocked_classifications=None,
                 blocked_unclassified=False):
    if suites is None:
        suites = [{"suite": "alpha_suite", "exe": "alpha_suite.exe",
                   "pass": passed, "fail": failed, "blocked": blocked,
                   "returncode": 0, "timed_out": False, "duration_ms": 10,
                   "process_startup_ms": 5.0, "actual_test_ms": 4.0,
                   "result_collection_ms": 1.0}]
    return {
        "orchestrator": "native-test-orchestrator/v2",
        "language": "csharp",
        "mode": "run",
        "verdict": verdict,
        "passed": passed,
        "failed": failed,
        "blocked": blocked,
        "cases": cases,
        "suites": suites,
        "failures": list(failures or []),
        "blocked_classifications": list(blocked_classifications or []),
        "blocked_unclassified": blocked_unclassified,
        "artifact_hash": "ab" * 32,
        "timing": {
            "total_gate_time_ms": 10.0,
            "process_startup_time_ms": 5.0,
            "actual_test_time_ms": 4.0,
            "result_collection_time_ms": 1.0,
        },
        "manifest_source": "manifest",
        "manifest_built_revision": "testrev",
        "source_revision": "testrev",
        "revision_match": True,
    }


def _orchestrator_writing(report):
    def run(argv, cwd: Path, timeout: float):
        (cwd / "native-orchestration-report.json").write_text(
            json.dumps(report), encoding="utf-8")
        return _completed(0)
    return run


def _cfg(**over):
    cfg = {"enabled": True, "auto_build": False, "suite_timeout_s": 5,
           "run_budget_s": 60, "lock_wait_s": 0}
    cfg.update(over)
    return cfg


def _gate(tmp_path, **kw):
    kw.setdefault("lock", _null_lock())
    return push_gate.mandatory_test_gate(tmp_path, **kw)


def test_gate_passes_on_green_suite(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    gate = _gate(
        tmp_path, orchestrator=_orchestrator_writing(_orch_report()),
        config=_cfg())
    assert gate["passed"] is True
    assert gate["totals"]["pass"] == 1
    assert gate["orchestrator"] == "native-test-orchestrator/v2"


def test_gate_invokes_orchestrator_with_governed_args(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    seen = {}

    def run(argv, cwd: Path, timeout: float):
        seen["argv"] = list(argv)
        seen["timeout"] = timeout
        return _orchestrator_writing(_orch_report())(argv, cwd, timeout)

    gate = _gate(tmp_path, orchestrator=run,
                 config=_cfg(max_parallel_suites=3, suite_timeout_s=9,
                             run_budget_s=42))
    assert gate["passed"] is True
    argv = seen["argv"]
    assert "--run" in argv
    assert "--require-manifest" in argv
    assert argv[argv.index("--max-parallel") + 1] == "3"
    assert argv[argv.index("--suite-timeout-s") + 1] == "9"
    assert seen["timeout"] == 42.0
    assert gate["max_parallel_suites"] == 3


def test_gate_records_timing_decomposition(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    gate = _gate(
        tmp_path, orchestrator=_orchestrator_writing(_orch_report()),
        config=_cfg())
    timing = gate["timing"]
    for key in ("total_gate_time_ms", "process_startup_time_ms",
                "actual_test_time_ms", "result_collection_time_ms"):
        assert key in timing
    assert gate["artifact_hash"] == "ab" * 32


def test_gate_denies_on_failed_case(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    report = _orch_report(
        passed=0, failed=1, verdict="FAIL",
        failures=["alpha_suite/bad: boom"])
    gate = _gate(
        tmp_path, orchestrator=_orchestrator_writing(report), config=_cfg())
    assert gate["passed"] is False
    assert "bad" in gate["detail"]


def test_gate_denies_on_fail_verdict(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    report = _orch_report(passed=1, verdict="FAIL",
                          failures=["alpha_suite/x: crash"])
    gate = _gate(
        tmp_path, orchestrator=_orchestrator_writing(report), config=_cfg())
    assert gate["passed"] is False


def test_gate_blocked_is_incomplete_not_denied(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    report = _orch_report(
        passed=1, blocked=1, cases=2, verdict="INCOMPLETE_EVIDENCE",
        blocked_classifications=[{
            "blocked_suite": "alpha_suite",
            "blocked_case": "env",
            "blocked_reason": "GPU unavailable",
            "affected_capability": "exploratory path",
            "release_impact": "none",
            "required_evidence": "eventual suite PASS",
            "criticality": "experimental",
        }])
    gate = _gate(
        tmp_path, orchestrator=_orchestrator_writing(report), config=_cfg())
    assert gate["passed"] is True
    assert gate["incomplete_evidence"] is True
    cls = gate["blocked_classifications"]
    assert len(cls) == 1
    assert cls[0]["criticality"] == "experimental"
    assert cls[0]["blocked_suite"] == "alpha_suite"
    assert cls[0]["blocked_case"] == "env"
    assert cls[0]["blocked_reason"] == "GPU unavailable"
    for field in ("required_evidence", "affected_capability",
                  "release_impact"):
        assert cls[0][field]


def test_gate_blocked_release_critical_denies(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    report = _orch_report(
        passed=1, blocked=1, cases=2, verdict="FAIL",
        blocked_classifications=[{
            "blocked_suite": "alpha_suite",
            "blocked_case": "abi",
            "blocked_reason": "toolchain absent",
            "affected_capability": "alpha capability",
            "release_impact": "alpha unverified",
            "required_evidence": "alpha_suite PASS report",
            "criticality": "release-critical",
        }])
    gate = _gate(
        tmp_path, orchestrator=_orchestrator_writing(report), config=_cfg())
    assert gate["passed"] is False
    assert gate["incomplete_evidence"] is True
    assert "blocked-release-critical" in gate["detail"]


def test_gate_blocked_unregistered_denies(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    report = _orch_report(
        passed=1, blocked=1, cases=2, verdict="FAIL",
        blocked_classifications=[{
            "blocked_suite": "alpha_suite",
            "blocked_case": "env",
            "blocked_reason": "",
            "affected_capability": "unregistered-suite",
            "release_impact": "unclassified",
            "required_evidence": "suite PASS report",
            "criticality": "release-critical",
        }])
    gate = _gate(
        tmp_path, orchestrator=_orchestrator_writing(report), config=_cfg())
    assert gate["passed"] is False
    assert "blocked-release-critical" in gate["detail"]


def test_gate_blocked_registry_missing_denies(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    report = _orch_report(
        passed=1, blocked=1, cases=2, verdict="FAIL",
        blocked_unclassified=True)
    gate = _gate(
        tmp_path, orchestrator=_orchestrator_writing(report), config=_cfg())
    assert gate["passed"] is False
    assert "blocked-unclassified" in gate["detail"]


def test_gate_denies_when_manifest_missing(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    (d / "suite-manifest.json").unlink()
    _exe(d)
    gate = _gate(tmp_path,
                 orchestrator=_orchestrator_writing(_orch_report()),
                 config=_cfg())
    assert gate["passed"] is False
    assert "stale-or-missing" in gate["detail"]


def test_gate_denies_when_manifest_revision_empty(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _manifest(d, revision="")
    _exe(d)
    gate = _gate(tmp_path,
                 orchestrator=_orchestrator_writing(_orch_report()),
                 config=_cfg())
    assert gate["passed"] is False
    assert "stale-or-missing" in gate["detail"]


def test_gate_records_source_revision(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    gate = _gate(
        tmp_path, orchestrator=_orchestrator_writing(_orch_report()),
        config=_cfg())
    assert gate["passed"] is True
    assert gate["source_revision"] == "testrev"


def test_gate_denies_on_missing_report(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    gate = _gate(tmp_path, orchestrator=lambda *a: _completed(0),
                 config=_cfg())
    assert gate["passed"] is False
    assert "report-missing" in gate["detail"]


def test_gate_denies_on_suite_crash(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    report = _orch_report(
        passed=1, failed=1, cases=1, verdict="FAIL",
        suites=[{"suite": "alpha_suite", "exe": "alpha_suite.exe",
                 "pass": 1, "fail": 0, "blocked": 0, "returncode": 2,
                 "timed_out": False, "duration_ms": 5}],
        failures=["alpha_suite/suite_exit: rc=2"])
    gate = _gate(
        tmp_path, orchestrator=_orchestrator_writing(report), config=_cfg())
    assert gate["passed"] is False
    assert "suite_exit" in gate["detail"]


def test_gate_denies_on_timeout(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)

    def run(*a):
        raise subprocess.TimeoutExpired(cmd="orch", timeout=5)

    gate = _gate(tmp_path, orchestrator=run, config=_cfg())
    assert gate["passed"] is False
    assert "run-budget-exceeded" in gate["detail"]


def test_gate_denies_on_orchestrator_spawn_error(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)

    def run(*a):
        raise OSError("no exe")

    gate = _gate(tmp_path, orchestrator=run, config=_cfg())
    assert gate["passed"] is False
    assert "orchestrator-error" in gate["detail"]


def test_gate_denies_when_orchestrator_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    monkeypatch.setattr(push_gate, "_ensure_orchestrator",
                        lambda *a, **k: None)
    gate = _gate(tmp_path, config=_cfg())
    assert gate["passed"] is False
    assert "orchestrator-unavailable" in gate["detail"]


def test_gate_denies_when_binaries_missing_no_autobuild(
    tmp_path: Path,
) -> None:
    _bin_dir(tmp_path)  # empty bin dir -> stale/missing
    gate = _gate(
        tmp_path, orchestrator=lambda *a: _completed(0),
        config=_cfg(auto_build=False))
    assert gate["passed"] is False
    assert "test-binaries-stale-or-missing" in gate["detail"]


def test_gate_rebuilds_via_builder(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    built: list[bool] = []

    def builder(timeout: float):
        built.append(True)
        _exe(d)
        return _completed(0)

    gate = _gate(
        tmp_path, orchestrator=_orchestrator_writing(_orch_report()),
        builder=builder,
        config=_cfg(auto_build=True),
    )
    assert built == [True]
    assert gate["rebuilt"] is True
    assert gate["passed"] is True


def test_gate_denies_on_build_failure(tmp_path: Path) -> None:
    _bin_dir(tmp_path)

    def builder(timeout: float):
        return _completed(1)

    gate = _gate(
        tmp_path, orchestrator=lambda *a: _completed(0), builder=builder,
        config=_cfg(auto_build=True),
    )
    assert gate["passed"] is False
    assert "build-failed" in gate["detail"]


def test_gate_denies_on_build_timeout(tmp_path: Path) -> None:
    _bin_dir(tmp_path)

    def builder(timeout: float):
        raise subprocess.TimeoutExpired(cmd="build", timeout=5)

    gate = _gate(tmp_path, orchestrator=lambda *a: _completed(0),
                 builder=builder, config=_cfg(auto_build=True))
    assert gate["passed"] is False
    assert gate["detail"] == "build-timeout"


def test_gate_disabled_records_but_denies(tmp_path: Path) -> None:
    gate = _gate(tmp_path, config=_cfg(enabled=False))
    assert gate["skipped"] is True
    assert gate["passed"] is False
    assert "enabled=false" in gate["detail"]


def test_gate_fails_closed_on_config_error(tmp_path: Path) -> None:
    gate = _gate(tmp_path, config={"config_error": "ValueError"})
    assert gate["passed"] is False
    assert "config-error" in gate["detail"]


def test_gate_denies_on_lock_busy(tmp_path: Path) -> None:
    @contextmanager
    def busy():
        raise LockBusyError("locked")
        yield

    gate = _gate(tmp_path, lock=busy(), config=_cfg())
    assert gate["passed"] is False
    assert "gate-busy" in gate["detail"]


def test_gate_denies_on_empty_cases(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    report = _orch_report(passed=0, failed=0, blocked=0, cases=0,
                          verdict="PASS")
    gate = _gate(
        tmp_path, orchestrator=_orchestrator_writing(report), config=_cfg())
    assert gate["passed"] is False
    assert "no-test-cases" in gate["detail"]


def test_gate_denies_on_run_budget(tmp_path: Path) -> None:
    import time

    d = _bin_dir(tmp_path)
    _exe(d)

    def slow_orchestrator(argv, cwd: Path, timeout: float):
        time.sleep(0.05)
        raise subprocess.TimeoutExpired(cmd="orch", timeout=timeout)

    gate = _gate(
        tmp_path, orchestrator=slow_orchestrator,
        config=_cfg(run_budget_s=0.01),
    )
    assert gate["passed"] is False
    assert "run-budget-exceeded" in gate["detail"]


def test_config_reads_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "automation-flows.json"
    manifest.write_text(json.dumps({
        "flows": {"git-automation": {"push_gate": {
            "enabled": True, "auto_build": False, "suite_timeout_s": 9,
        }}}
    }), encoding="utf-8")
    monkeypatch.setattr(push_gate, "_FLOWS_CONFIG", manifest)
    cfg = push_gate.push_gate_config()
    assert cfg["enabled"] is True
    assert cfg["auto_build"] is False
    assert cfg["suite_timeout_s"] == 9.0


def test_config_malformed_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "automation-flows.json"
    manifest.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(push_gate, "_FLOWS_CONFIG", manifest)
    cfg = push_gate.push_gate_config()
    assert cfg["config_error"]
    assert cfg["enabled"] is True


def test_convergence_evidence_records_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "gptbridge-push-gate.json"
    monkeypatch.setattr(push_gate, "_state_path", lambda root: state_file)

    class FakeRepo:
        def __init__(self, path):  # noqa: ANN001
            self.path = Path(path)

        def run(self, args):  # noqa: ANN001
            joined = " ".join(args)
            if "rev-list" in joined:
                return _completed(0, "0")
            if "rev-parse" in joined:
                return _completed(0, "abc123\n")
            return _completed(0, "")

    monkeypatch.setattr(push_gate, "GitRepository", FakeRepo)
    import governance_rule.execution.git_tiers.repo_sync as repo_sync
    monkeypatch.setattr(repo_sync, "GitRepository", FakeRepo)
    import governance_rule.execution.git_tiers as git_tiers_pkg
    monkeypatch.setattr(git_tiers_pkg, "audit_log", lambda *a, **k: {"ok": True})

    evidence = push_gate.record_convergence_evidence(tmp_path, actor="t")
    assert evidence["state"] == "IN_SYNC"
    assert evidence["ahead"] == 0
    assert evidence["consecutive_in_sync"] == 1
    written = json.loads(state_file.read_text(encoding="utf-8"))
    assert written["convergence"]["ahead_zero"] is True

    push_gate.record_convergence_evidence(tmp_path, actor="t")
    written = json.loads(state_file.read_text(encoding="utf-8"))
    assert written["convergence"]["consecutive_in_sync"] == 2


def test_push_evidence_denial_and_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_file = tmp_path / "gptbridge-push-gate.json"
    monkeypatch.setattr(push_gate, "_state_path", lambda root: state_file)

    class FakeRepo:
        def __init__(self, path):  # noqa: ANN001
            self.path = Path(path)

        def run(self, args):  # noqa: ANN001
            return _completed(0, "deadbeef\n")

    monkeypatch.setattr(push_gate, "GitRepository", FakeRepo)
    records = []
    import governance_rule.execution.git_tiers as git_tiers_pkg
    monkeypatch.setattr(
        git_tiers_pkg, "audit_log", lambda *a, **k: records.append(k) or {"ok": True},
    )
    gate = {"passed": False, "skipped": False, "returncode": 1,
            "duration_ms": 5, "suites": []}
    push_gate.record_push_evidence(
        tmp_path, actor="t", test_gate=gate, pushed=False, detail="deny",
    )
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["last_push"]["result"] == "denied"
    assert records and records[0]["operation"] == "push"
    assert records[0]["result"] == "denied"


def test_config_max_parallel_suites_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "automation-flows.json"
    monkeypatch.setattr(push_gate, "_FLOWS_CONFIG", manifest)

    def cfg_for(value):
        manifest.write_text(json.dumps({
            "flows": {"git-automation": {"push_gate": {
                "max_parallel_suites": value}}}}
        ), encoding="utf-8")
        return push_gate.push_gate_config()["max_parallel_suites"]

    assert cfg_for(2) == 2
    assert cfg_for(100) == push_gate.MAX_PARALLEL_SUITES_CAP
    assert cfg_for(0) == push_gate.DEFAULT_MAX_PARALLEL_SUITES
    assert cfg_for(-3) == push_gate.DEFAULT_MAX_PARALLEL_SUITES
    assert cfg_for("x") == push_gate.DEFAULT_MAX_PARALLEL_SUITES
    assert cfg_for(True) == push_gate.DEFAULT_MAX_PARALLEL_SUITES
