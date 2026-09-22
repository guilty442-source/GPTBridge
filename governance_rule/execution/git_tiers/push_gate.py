"""Pre-push mandatory test gate + push/convergence audit evidence (§10.69).

§10.69-C① push preconditions: all worktrees clean, governance audit PASS,
**mandatory tests PASS**, and ``origin/main`` an ancestor of local ``main``.
The test gate is the missing piece: a bounded pytest selection run against
the integrated ``main`` worktree immediately before ``git push``.  A gate
failure denies the push (fail-closed) and is audited like every other gate.

§10.69-C④/F④: every push attempt (granted or denied) records a consolidated
decision record — gate outcomes, revisions, result — into the governed audit
ledger, and §10.69-F①/D④: every synchronization records periodic
``ahead==0`` convergence evidence (ledger + small state file under the git
common dir) so "local main == origin/main" is continuously provable, not
only at release time.

Config: ``main-system/config/automation-flows.json`` →
``flows.git-automation.push_gate`` (the git-automation flow tunables root):

    {"enabled": true,
     "mandatory_tests": ["governance_rule/execution/git_tiers/tests"],
     "test_timeout_s": 300}

``enabled=false`` skips the test run but is recorded in the evidence record
(governed-config choice, never silent).  An unreadable or malformed manifest
fails closed: the gate reports ``config-error`` and the push is denied.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .git_repository import GitRepository

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_FLOWS_CONFIG = (
    PROJECT_ROOT / "main-system" / "config" / "automation-flows.json"
)
_STATE_FILENAME = "gptbridge-push-gate.json"
_STATE_SCHEMA = "gptbridge-push-gate/v1"

# Bounded default: the git-tier suite guards exactly what the push gate
# protects (tier enforcement, self-commit, worktree/merge safety).  The
# manifest list replaces it wholesale — there is no implicit union.
DEFAULT_MANDATORY_TESTS: tuple[str, ...] = (
    "governance_rule/execution/git_tiers/tests",
    "main-system/tests/test_git_tier_governance.py",
)
DEFAULT_TEST_TIMEOUT_S: float = 300.0

Runner = Callable[..., Any]


def push_gate_config() -> dict[str, Any]:
    """Push-gate tunables from the git-automation flow manifest.

    Fail-closed: an unreadable/malformed manifest yields ``config_error``
    set (callers must treat the gate as denied).  ``enabled`` defaults to
    True — the gate is mandatory unless a governed edit turns it off.
    """
    defaults: dict[str, Any] = {
        "enabled": True,
        "mandatory_tests": list(DEFAULT_MANDATORY_TESTS),
        "test_timeout_s": DEFAULT_TEST_TIMEOUT_S,
    }
    try:
        raw = json.loads(_FLOWS_CONFIG.read_text(encoding="utf-8"))
        entry = (
            raw.get("flows", {}).get("git-automation", {}).get("push_gate")
        )
    except (OSError, ValueError, AttributeError) as exc:
        defaults["config_error"] = f"{type(exc).__name__}"
        return defaults
    if not isinstance(entry, Mapping):
        return defaults
    merged = dict(defaults)
    if "enabled" in entry:
        merged["enabled"] = bool(entry.get("enabled"))
    tests = entry.get("mandatory_tests")
    if isinstance(tests, (list, tuple)) and tests:
        merged["mandatory_tests"] = [str(t) for t in tests]
    timeout = entry.get("test_timeout_s")
    if isinstance(timeout, (int, float)) and timeout > 0:
        merged["test_timeout_s"] = float(timeout)
    return merged


def mandatory_test_gate(
    root: str | Path,
    *,
    runner: Runner | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the configured mandatory tests against ``root`` (bounded).

    Returns a gate record: ``passed`` is False on any test failure,
    timeout, missing interpreter, or config error — the push path treats
    every non-pass as a denial (fail-closed).
    """
    cfg = dict(config) if config is not None else push_gate_config()
    gate: dict[str, Any] = {
        "gate": "mandatory-tests",
        "passed": False,
        "skipped": False,
        "tests": list(cfg.get("mandatory_tests") or ()),
        "timeout_s": float(cfg.get("test_timeout_s") or DEFAULT_TEST_TIMEOUT_S),
        "returncode": None,
        "duration_ms": 0,
        "detail": "",
    }
    if cfg.get("config_error"):
        gate["detail"] = f"config-error:{cfg['config_error']}"
        return gate
    if not cfg.get("enabled", True):
        gate["skipped"] = True
        gate["detail"] = "push_gate.enabled=false (governed config)"
        return gate
    if not gate["tests"]:
        gate["detail"] = "no-mandatory-tests-configured"
        return gate

    run = runner or _pytest_runner
    started = time.monotonic()
    try:
        result = run(root, gate["tests"], gate["timeout_s"])
    except Exception as exc:  # noqa: BLE001 - gate must total to a verdict
        gate["duration_ms"] = int((time.monotonic() - started) * 1000)
        gate["detail"] = f"runner-error:{type(exc).__name__}"
        return gate
    gate["duration_ms"] = int((time.monotonic() - started) * 1000)
    rc = getattr(result, "returncode", None)
    gate["returncode"] = int(rc) if rc is not None else -1
    tail = str(getattr(result, "stderr", "") or getattr(result, "stdout", ""))
    gate["detail"] = tail.strip()[-300:]
    gate["passed"] = gate["returncode"] == 0
    return gate


def _pytest_runner(root: str | Path, tests: list[str], timeout_s: float) -> Any:
    """Bounded pytest run on the repo root (pytest.ini supplies -n/timeout)."""
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *tests],
        cwd=Path(root),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(5.0, float(timeout_s)),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _state_path(root: str | Path) -> Path:
    """State file lives next to the workspace-sync lock (git common dir)."""
    repo = GitRepository(root)
    result = repo.run(["rev-parse", "--git-common-dir"])
    raw = (result.stdout or "").strip()
    common = Path(raw)
    if not common.is_absolute():
        common = repo.path / common
    return common.resolve() / _STATE_FILENAME


def _read_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(path: Path, state: Mapping[str, Any]) -> None:
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except OSError:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _ahead_behind(repo: GitRepository) -> tuple[int | None, int | None]:
    """(ahead, behind) of local main vs origin/main; None when unknown."""
    ahead = repo.run(["rev-list", "--count", "origin/main..main"])
    behind = repo.run(["rev-list", "--count", "main..origin/main"])
    try:
        ahead_n = int((ahead.stdout or "").strip()) if ahead.returncode == 0 else None
        behind_n = int((behind.stdout or "").strip()) if behind.returncode == 0 else None
    except (TypeError, ValueError):
        ahead_n = behind_n = None
    return ahead_n, behind_n


def record_convergence_evidence(
    root: str | Path, *, actor: str, pushed: bool = False
) -> dict[str, Any]:
    """Periodic ``ahead==0`` proof: ledger entry + convergence state (F①/D④).

    Records every synchronization outcome: the local↔origin relation,
    ahead/behind counts and a running ``consecutive_in_sync`` streak.
    Ledger entries make the proof continuous; the state file is the
    latest snapshot for status surfaces.
    """
    from . import audit_log
    from .repo_sync import sync_state

    repo = GitRepository(root)
    state = sync_state(root)
    ahead_n, behind_n = _ahead_behind(repo)
    in_sync = state["state"] == "IN_SYNC" and ahead_n == 0

    path = _state_path(root)
    prior = _read_state(path)
    streak = int(prior.get("convergence", {}).get("consecutive_in_sync") or 0)
    streak = streak + 1 if in_sync else 0

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    convergence = {
        "state": state["state"],
        "ahead": ahead_n,
        "behind": behind_n,
        "ahead_zero": in_sync,
        "consecutive_in_sync": streak,
        "last_checked": now,
        "local_main_sha": state["local_main_sha"],
        "origin_main_sha": state["origin_main_sha"],
    }
    record = dict(prior)
    record.update(
        {
            "schema": _STATE_SCHEMA,
            "updated_at": now,
            "convergence": convergence,
        }
    )
    _write_state(path, record)

    audit_log(
        1,
        "sync-state origin/main",
        actor,
        True,
        f"sync_state={state['state']} ahead={ahead_n} behind={behind_n}"
        f" streak={streak} pushed={pushed}",
        operation="convergence-check",
        phase="result",
        result="in-sync" if in_sync else state["state"].lower(),
        returncode=0 if in_sync else 1,
    )
    return convergence


def record_push_evidence(
    root: str | Path,
    *,
    actor: str,
    test_gate: Mapping[str, Any] | None,
    pushed: bool,
    detail: str = "",
) -> dict[str, Any]:
    """Consolidated push decision record (C④/F④): ledger + state file.

    One record carries the mandatory-test gate outcome plus the revisions
    involved, so a push is provably preceded by audit PASS + test PASS.
    """
    from . import audit_log

    repo = GitRepository(root)
    local_sha = (
        repo.run(["rev-parse", "main"]).stdout or ""
    ).strip()
    origin_sha = (
        repo.run(["rev-parse", "origin/main"]).stdout or ""
    ).strip()
    gate_summary = "none"
    if test_gate is not None:
        gate_summary = (
            f"tests={'pass' if test_gate.get('passed') else 'fail'}"
            f" skipped={bool(test_gate.get('skipped'))}"
            f" rc={test_gate.get('returncode')}"
            f" ms={test_gate.get('duration_ms')}"
        )
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    path = _state_path(root)
    record = _read_state(path)
    record.update(
        {
            "schema": _STATE_SCHEMA,
            "updated_at": now,
            "last_test_gate": dict(test_gate) if test_gate else None,
            "last_push": {
                "at": now,
                "result": "pushed" if pushed else "denied",
                "local_main_sha": local_sha,
                "origin_main_sha": origin_sha,
                "test_gate": gate_summary,
                "detail": detail[:300],
            },
        }
    )
    _write_state(path, record)

    audit_log(
        2,
        "push origin main",
        actor,
        pushed,
        f"mandatory-test-gate[{gate_summary}] {detail}".strip()[:500],
        operation="push",
        phase="result",
        result="pushed" if pushed else "denied",
        returncode=0 if pushed else 1,
    )
    return record


__all__ = [
    "DEFAULT_MANDATORY_TESTS",
    "DEFAULT_TEST_TIMEOUT_S",
    "mandatory_test_gate",
    "push_gate_config",
    "record_convergence_evidence",
    "record_push_evidence",
]
