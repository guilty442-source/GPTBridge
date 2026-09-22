"""Push gate tests (§10.69 C/F): mandatory native-test gate, evidence records."""
from __future__ import annotations

import contextlib
import json
import subprocess
import time
from pathlib import Path

import pytest

from governance_rule.execution.git_tiers import push_gate

NULL_LOCK = contextlib.nullcontext()


def _completed(returncode: int, out: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=["suite"], returncode=returncode,
                                       stdout=out, stderr="")


def _mk_suites(root: Path, names=("alpha_suite", "beta_suite")) -> Path:
    bin_dir = root / "native" / "test_suites" / "bin"
    bin_dir.mkdir(parents=True)
    for name in names:
        (bin_dir / f"{name}.exe").write_bytes(b"MZ")
    return bin_dir


def _runner_report(passed=1, failed=0, blocked=0, rc=0):
    def run(exe: Path, cwd: Path, timeout_s: float) -> subprocess.CompletedProcess:
        cases = (
            [{"suite": exe.stem, "name": "ok", "status": "PASS"}] * passed
            + [{"suite": exe.stem, "name": "bad", "status": "FAIL",
                "detail": "boom"}] * failed
            + [{"suite": exe.stem, "name": "skip", "status": "BLOCKED"}] * blocked
        )
        (cwd / f"{exe.stem}.json").write_text(
            json.dumps({"cases": cases, "passed": passed,
                        "failed": failed, "blocked": blocked}),
            encoding="utf-8")
        return _completed(rc)
    return run


def _cfg(**over) -> dict:
    base = {"enabled": True, "auto_build": False, "suite_timeout_s": 5,
            "run_budget_s": 60, "lock_wait_s": 1}
    base.update(over)
    return base


def test_gate_passes_on_green(tmp_path: Path) -> None:
    _mk_suites(tmp_path)
    gate = push_gate.mandatory_test_gate(
        tmp_path, runner=_runner_report(), config=_cfg(), lock=NULL_LOCK,
    )
    assert gate["passed"] is True
    assert gate["totals"]["pass"] == 2
    assert len(gate["suites"]) == 2


def test_gate_denies_on_case_failure(tmp_path: Path) -> None:
    _mk_suites(tmp_path)
    gate = push_gate.mandatory_test_gate(
        tmp_path, runner=_runner_report(passed=0, failed=1, rc=1),
        config=_cfg(), lock=NULL_LOCK,
    )
    assert gate["passed"] is False
    assert gate["totals"]["fail"] == 2
    assert "bad" in gate["failures"][0]


def test_gate_blocked_is_incomplete_not_fail(tmp_path: Path) -> None:
    _mk_suites(tmp_path, names=("only_suite",))
    gate = push_gate.mandatory_test_gate(
        tmp_path, runner=_runner_report(passed=0, blocked=1),
        config=_cfg(), lock=NULL_LOCK,
    )
    assert gate["passed"] is True
    assert gate["incomplete_evidence"] is True
    assert gate["totals"]["blocked"] == 1


def test_gate_denies_when_binaries_missing_and_no_autobuild(
    tmp_path: Path,
) -> None:
    (tmp_path / "native" / "test_suites" / "bin").mkdir(parents=True)
    gate = push_gate.mandatory_test_gate(
        tmp_path, config=_cfg(), lock=NULL_LOCK,
    )
    assert gate["passed"] is False
    assert "stale-or-missing" in gate["detail"]


def test_gate_rebuilds_stale_binaries(tmp_path: Path) -> None:
    bin_dir = _mk_suites(tmp_path, names=("only_suite",))
    src = tmp_path / "native" / "core"
    src.mkdir(parents=True)
    (src / "runtime_core.c").write_text("int x;", encoding="utf-8")
    exe = bin_dir / "only_suite.exe"
    old = time.time() - 3600
    import os
    os.utime(exe, (old, old))

    built = {"called": False}

    def builder(timeout_s: float):
        built["called"] = True
        now = time.time()
        os.utime(exe, (now, now))
        return _completed(0)

    gate = push_gate.mandatory_test_gate(
        tmp_path, runner=_runner_report(), builder=builder,
        config=_cfg(auto_build=True), lock=NULL_LOCK,
    )
    assert built["called"] is True
    assert gate["rebuilt"] is True
    assert gate["passed"] is True


def test_gate_denies_on_failed_build(tmp_path: Path) -> None:
    bin_dir = _mk_suites(tmp_path, names=("only_suite",))
    src = tmp_path / "native" / "core"
    src.mkdir(parents=True)
    (src / "runtime_core.c").write_text("int x;", encoding="utf-8")
    exe = bin_dir / "only_suite.exe"
    old = time.time() - 3600
    import os
    os.utime(exe, (old, old))

    gate = push_gate.mandatory_test_gate(
        tmp_path, runner=_runner_report(),
        builder=lambda timeout_s: _completed(1),
        config=_cfg(auto_build=True), lock=NULL_LOCK,
    )
    assert gate["passed"] is False
    assert "build-failed" in gate["detail"]


def test_gate_denies_on_suite_timeout(tmp_path: Path) -> None:
    _mk_suites(tmp_path, names=("slow_suite",))

    def timeout_runner(exe, cwd, timeout_s):
        raise subprocess.TimeoutExpired(cmd=str(exe), timeout=timeout_s)

    gate = push_gate.mandatory_test_gate(
        tmp_path, runner=timeout_runner, config=_cfg(), lock=NULL_LOCK,
    )
    assert gate["passed"] is False
    assert gate["totals"]["fail"] == 1


def test_gate_denies_on_missing_report(tmp_path: Path) -> None:
    _mk_suites(tmp_path, names=("quiet_suite",))
    gate = push_gate.mandatory_test_gate(
        tmp_path, runner=lambda exe, cwd, t: _completed(0),
        config=_cfg(), lock=NULL_LOCK,
    )
    assert gate["passed"] is False
    assert gate["totals"]["fail"] == 1


def test_gate_disabled_is_recorded_and_denied(tmp_path: Path) -> None:
    gate = push_gate.mandatory_test_gate(
        tmp_path, config={"enabled": False}, lock=NULL_LOCK,
    )
    assert gate["skipped"] is True
    assert gate["passed"] is False


def test_gate_fails_closed_on_config_error(tmp_path: Path) -> None:
    gate = push_gate.mandatory_test_gate(
        tmp_path, config={"enabled": True, "config_error": "ValueError"},
        lock=NULL_LOCK,
    )
    assert gate["passed"] is False
    assert "config-error" in gate["detail"]


def test_config_reads_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = tmp_path / "automation-flows.json"
    manifest.write_text(json.dumps({
        "flows": {"git-automation": {"push_gate": {
            "enabled": True, "auto_build": False,
            "suite_timeout_s": 12, "run_budget_s": 45,
        }}}
    }), encoding="utf-8")
    monkeypatch.setattr(push_gate, "_FLOWS_CONFIG", manifest)
    cfg = push_gate.push_gate_config()
    assert cfg["enabled"] is True
    assert cfg["auto_build"] is False
    assert cfg["suite_timeout_s"] == 12.0
    assert cfg["run_budget_s"] == 45.0


def test_config_malformed_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest = tmp_path / "automation-flows.json"
    manifest.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(push_gate, "_FLOWS_CONFIG", manifest)
    cfg = push_gate.push_gate_config()
    assert cfg["config_error"]
    assert cfg["enabled"] is True  # gate still mandatory -> deny on error


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
    gate = {"passed": False, "skipped": False, "duration_ms": 5,
            "suites": ["only_suite"], "totals": {"pass": 0, "fail": 1,
                                               "blocked": 0}}
    push_gate.record_push_evidence(
        tmp_path, actor="t", test_gate=gate, pushed=False, detail="deny",
    )
    data = json.loads(state_file.read_text(encoding="utf-8"))
    assert data["last_push"]["result"] == "denied"
    assert "fail=1" in data["last_push"]["test_gate"]
    assert records and records[0]["operation"] == "push"
    assert records[0]["result"] == "denied"
