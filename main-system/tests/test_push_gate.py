"""Push gate tests (§10.69 C/F): native-suite gate + evidence records."""
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
    return subprocess.CompletedProcess(args=["suite"], returncode=returncode,
                                       stdout=out, stderr="")


def _bin_dir(root: Path) -> Path:
    d = root / "native" / "test_suites" / "bin"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _exe(bin_dir: Path, name: str = "alpha_suite.exe") -> Path:
    p = bin_dir / name
    p.write_bytes(b"MZ")
    return p


def _report(passed=1, failed=0, blocked=0, cases=None):
    if cases is None:
        cases = [{"suite": "alpha_suite", "name": "case1",
                  "status": "PASS", "detail": ""}]
    return {"passed": passed, "failed": failed, "blocked": blocked,
            "cases": cases}


def _runner_writing(report):
    def run(exe: Path, cwd: Path, timeout: float):
        (cwd / f"{exe.stem}.json").write_text(
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
    gate = _gate(tmp_path, runner=_runner_writing(_report()), config=_cfg())
    assert gate["passed"] is True
    assert gate["totals"]["pass"] == 1


def test_gate_denies_on_failed_case(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    report = _report(passed=0, failed=1, cases=[
        {"suite": "alpha_suite", "name": "bad", "status": "FAIL",
         "detail": "boom"}])
    gate = _gate(tmp_path, runner=_runner_writing(report), config=_cfg())
    assert gate["passed"] is False
    assert "bad" in gate["detail"]


def test_gate_blocked_is_incomplete_not_denied(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    report = _report(passed=1, blocked=1, cases=[
        {"suite": "alpha_suite", "name": "ok", "status": "PASS"},
        {"suite": "alpha_suite", "name": "env", "status": "BLOCKED"}])
    gate = _gate(tmp_path, runner=_runner_writing(report), config=_cfg())
    assert gate["passed"] is True
    assert gate["incomplete_evidence"] is True


def test_gate_denies_on_missing_report(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)
    gate = _gate(tmp_path, runner=lambda *a: _completed(0), config=_cfg())
    assert gate["passed"] is False
    assert "report-missing" in gate["detail"]


def test_gate_denies_on_suite_crash(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)

    def run(exe: Path, cwd: Path, timeout: float):
        (cwd / f"{exe.stem}.json").write_text(
            json.dumps(_report()), encoding="utf-8")
        return _completed(2)

    gate = _gate(tmp_path, runner=run, config=_cfg())
    assert gate["passed"] is False
    assert "suite_exit" in gate["detail"]


def test_gate_denies_on_timeout(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d)

    def run(*a):
        raise subprocess.TimeoutExpired(cmd="suite", timeout=5)

    gate = _gate(tmp_path, runner=run, config=_cfg())
    assert gate["passed"] is False
    assert "timeout" in gate["detail"]


def test_gate_denies_when_binaries_missing_no_autobuild(
    tmp_path: Path,
) -> None:
    _bin_dir(tmp_path)  # empty bin dir -> stale/missing
    gate = _gate(tmp_path, config=_cfg(auto_build=False))
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
        tmp_path, runner=_runner_writing(_report()), builder=builder,
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
        tmp_path, builder=builder, config=_cfg(auto_build=True),
    )
    assert gate["passed"] is False
    assert "build-failed" in gate["detail"]


def test_gate_denies_on_build_timeout(tmp_path: Path) -> None:
    _bin_dir(tmp_path)

    def builder(timeout: float):
        raise subprocess.TimeoutExpired(cmd="build", timeout=5)

    gate = _gate(tmp_path, builder=builder, config=_cfg(auto_build=True))
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
    gate = _gate(
        tmp_path,
        runner=_runner_writing({"passed": 0, "failed": 0, "blocked": 0,
                                "cases": []}),
        config=_cfg(),
    )
    assert gate["passed"] is False
    assert "no-test-cases" in gate["detail"]


def test_gate_denies_on_run_budget(tmp_path: Path) -> None:
    d = _bin_dir(tmp_path)
    _exe(d, "a_suite.exe")
    _exe(d, "b_suite.exe")
    gate = _gate(
        tmp_path, runner=_runner_writing(_report()),
        config=_cfg(run_budget_s=0.0001),
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

            if "rev-parse" in joined:
                return _completed(0, "abc123
")
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
            return _completed(0, "deadbeef
")

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
