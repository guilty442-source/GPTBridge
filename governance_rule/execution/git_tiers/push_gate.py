"""Pre-push mandatory native test gate + push/convergence audit evidence (§10.69).

§10.69-C① push preconditions: all worktrees clean, governance audit PASS,
**mandatory tests PASS**, and ``origin/main`` an ancestor of local ``main``.
The test gate is the canonical native suite — per the 2026-09-22 governor
directive (§1.1) test/audit verdicts come only from the main-system suites
(``native/test_suites`` C tests + C/C++ audit engine); **pytest is never a
gate**.  The gate therefore runs the ``*_suite.exe`` binaries built by the
canonical ``native/test_suites/build.ps1`` harness:

- binaries stale against their link inputs (or missing) → one bounded
  rebuild through ``build.ps1`` (it owns the suite→source link map; no
  second build recipe is duplicated here);
- every suite then executes in the shared ``bin/`` directory with a
  per-suite bound and a whole-run budget (§3.1: suite time over ~30 s is a
  defect, not a timeout to raise); suites run with bounded parallelism
  (``max_parallel_suites``, default 4, hard cap 8 — each suite writes a
  uniquely-named ``<stem>.json`` report so parallel runs cannot interleave;
  suites bind ephemeral ports, never fixed ones);
- any ``FAIL`` case, crash, stale report, build failure or unavailable
  toolchain denies the push (fail-closed); ``BLOCKED`` cases are the
  suite's own environmental-abstain verdict — they are recorded as
  ``incomplete_evidence`` but do not deny (matching the native
  orchestrator's INCOMPLETE_EVIDENCE ≠ FAIL semantics).

§10.69-C④/F④: every push attempt (granted or denied) records a consolidated
decision record — gate outcomes, revisions, result — into the governed audit
ledger, and §10.69-F①/D④: every synchronization records periodic
``ahead==0`` convergence evidence (ledger + small state file under the git
common dir) so "local main == origin/main" is continuously provable, not
only at release time.

Config: ``main-system/config/automation-flows.json`` →
``flows.git-automation.push_gate`` (the git-automation flow tunables root):

    {"enabled": true,
     "auto_build": true,
     "build_timeout_s": 600,
     "suite_timeout_s": 30,
     "run_budget_s": 120,
     "lock_wait_s": 30,
     "max_parallel_suites": 4}

``enabled=false`` records the governed decision but still denies the push:
the mandatory-test PASS precondition (C①) cannot be configured away, only
recorded.  An unreadable or malformed manifest fails closed: the gate
reports ``config-error`` and the push is denied.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping

from . import branch_policy
from .git_repository import GitRepository
from .process_lock import LockBusyError, ProcessFileLock

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_FLOWS_CONFIG = (
    PROJECT_ROOT / "main-system" / "config" / "automation-flows.json"
)
_STATE_FILENAME = "gptbridge-push-gate.json"
_STATE_SCHEMA = "gptbridge-push-gate/v1"
_LOCK_FILENAME = "push-test-gate.lock"

# Canonical native suite locations (single source: native/test_suites).
_NATIVE_TEST_DIR = Path("native") / "test_suites"
_BUILD_SCRIPT = _NATIVE_TEST_DIR / "build.ps1"
_BIN_DIR = _NATIVE_TEST_DIR / "bin"
_SUITE_GLOB = "*_suite.exe"
_REPORT_SUFFIX = ".json"

# Link inputs that invalidate cached suite binaries (build.ps1 $suites map
# roots): a newer code file in any of these means the binaries no longer
# test the tree that would be pushed.
_DEP_ROOTS = (
    _NATIVE_TEST_DIR,
    Path("native") / "core",
    Path("native") / "include",
    Path("native") / "tool_runtime",
    Path("native") / "audit",
    Path("Standalone tools")
    / branch_policy.LOCAL_MODEL_BRANCH
    / "src"
    / "backend"
    / "cpp",
)
_CODE_SUFFIXES = frozenset({".c", ".cpp", ".h", ".hpp"})

DEFAULT_BUILD_TIMEOUT_S = 600.0
DEFAULT_SUITE_TIMEOUT_S = 30.0
DEFAULT_RUN_BUDGET_S = 120.0
DEFAULT_LOCK_WAIT_S = 30.0
DEFAULT_MAX_PARALLEL_SUITES = 4
# Hard ceiling on suite parallelism regardless of config: suites share the
# bin/ directory and the host's CPU/IO, so an absurd configured bound would
# only trade flake for speed.
MAX_PARALLEL_SUITES_CAP = 8

Runner = Callable[..., Any]
Builder = Callable[..., Any]


def push_gate_config() -> dict[str, Any]:
    """Push-gate tunables from the git-automation flow manifest.

    Fail-closed: an unreadable/malformed manifest yields ``config_error``
    set (callers must treat the gate as denied).  ``enabled`` defaults to
    True — the gate is mandatory; disabling it is recorded but still
    denies the push (C① precondition cannot be configured away).
    """
    defaults: dict[str, Any] = {
        "enabled": True,
        "auto_build": True,
        "build_timeout_s": DEFAULT_BUILD_TIMEOUT_S,
        "suite_timeout_s": DEFAULT_SUITE_TIMEOUT_S,
        "run_budget_s": DEFAULT_RUN_BUDGET_S,
        "lock_wait_s": DEFAULT_LOCK_WAIT_S,
        "max_parallel_suites": DEFAULT_MAX_PARALLEL_SUITES,
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
    for key in ("enabled", "auto_build"):
        if key in entry:
            merged[key] = bool(entry.get(key))
    for key in (
        "build_timeout_s", "suite_timeout_s", "run_budget_s", "lock_wait_s",
    ):
        value = entry.get(key)
        if isinstance(value, (int, float)) and value > 0:
            merged[key] = float(value)
    parallel = entry.get("max_parallel_suites")
    if (
        isinstance(parallel, (int, float))
        and not isinstance(parallel, bool)
        and parallel >= 1
    ):
        merged["max_parallel_suites"] = min(
            MAX_PARALLEL_SUITES_CAP, int(parallel)
        )
    return merged


def _suite_exes(bin_dir: Path) -> list[Path]:
    """Suite binaries; ``_``-prefixed helpers are not suites."""
    try:
        return sorted(
            p for p in bin_dir.glob(_SUITE_GLOB)
            if p.is_file() and not p.name.startswith("_")
        )
    except OSError:
        return []


def _newest_source_mtime(root: Path) -> float:
    """Newest mtime across the suite link inputs (0 when none found)."""
    newest = 0.0
    for rel in _DEP_ROOTS:
        dep_root = Path(root) / rel
        if not dep_root.is_dir():
            continue
        try:
            for path in dep_root.rglob("*"):
                if (
                    path.is_file()
                    and path.suffix.lower() in _CODE_SUFFIXES
                ):
                    newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest


def _binaries_stale(root: Path, exes: list[Path]) -> bool:
    """True when any link input is newer than the oldest suite binary."""
    if not exes:
        return True
    oldest = min(exe.stat().st_mtime for exe in exes)
    return _newest_source_mtime(root) > oldest


def _powershell() -> str:
    return os.environ.get("GPTBRIDGE_POWERSHELL", "powershell.exe")


def _build_suites(root: Path, timeout_s: float, builder: Builder | None) -> Any:
    """One bounded rebuild via the canonical build.ps1 (returns proc)."""
    script = Path(root) / _BUILD_SCRIPT
    run = builder or (
        lambda timeout: subprocess.run(
            [
                _powershell(), "-NoProfile", "-ExecutionPolicy", "Bypass",
                "-File", str(script),
            ],
            cwd=Path(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    )
    return run(timeout_s)


def _run_suite(exe: Path, bin_dir: Path, timeout_s: float, runner: Runner) -> Any:
    """Run one suite binary; it writes ``<stem>.json`` into ``bin/``."""
    return runner(exe, bin_dir, timeout_s)


def _default_runner(exe: Path, cwd: Path, timeout_s: float) -> Any:
    return subprocess.run(
        [str(exe), exe.stem],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout_s,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _suite_report(bin_dir: Path, exe: Path) -> dict[str, Any]:
    """Parse the suite's ``<stem>.json`` report."""
    path = bin_dir / f"{exe.stem}{_REPORT_SUFFIX}"
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _execute_suite(
    exe: Path, bin_dir: Path, suite_timeout: float, runner: Runner
) -> dict[str, Any]:
    """Run one suite binary and return its outcome — worker-thread safe.

    Touches only the suite's own ``<stem>.json`` report and returns all
    findings in the result dict; the caller thread owns gate aggregation.
    Never raises: every failure mode is folded into a FAIL case so the
    orchestrator stays fail-closed.
    """
    suite_error = ""
    # Remove the previous report first: existence afterwards is the
    # freshness proof — mtime comparison is unreliable on filesystems with
    # coarse timestamp granularity.
    report_path = bin_dir / f"{exe.stem}{_REPORT_SUFFIX}"
    try:
        report_path.unlink(missing_ok=True)
    except OSError:
        pass
    proc = None
    try:
        proc = _run_suite(exe, bin_dir, suite_timeout, runner)
    except subprocess.TimeoutExpired:
        suite_error = f"timeout>{suite_timeout}s"
    except Exception as exc:  # noqa: BLE001 — fail closed
        suite_error = f"spawn-error:{type(exc).__name__}"
    report = _suite_report(bin_dir, exe)
    cases = report.get("cases") if report else None
    if not isinstance(cases, list):
        suite_error = (
            suite_error
            if proc is None
            else f"report-missing:rc={proc.returncode}"
        )
        cases = [
            {
                "suite": exe.stem,
                "name": "suite_execution",
                "status": "FAIL",
                "detail": suite_error,
            }
        ]
        report = {"passed": 0, "failed": 1, "blocked": 0}
    entry = {
        "suite": exe.stem,
        "pass": int(report.get("passed", 0)) if report else 0,
        "fail": int(report.get("failed", 0)) if report else 0,
        "blocked": int(report.get("blocked", 0)) if report else 0,
        "returncode": getattr(proc, "returncode", None) if proc else None,
    }
    return {"entry": entry, "cases": cases}


@contextmanager
def _gate_lock(root: Path, wait_s: float) -> Iterator[None]:
    """Serialize gate runs: suite binaries write per-suite reports into the
    shared ``bin/`` directory, so concurrent runs would interleave."""
    repo = GitRepository(root)
    result = repo.run(["rev-parse", "--git-common-dir"])
    raw = (result.stdout or "").strip()
    common = Path(raw)
    if not common.is_absolute():
        common = repo.path / common
    lock_path = common.resolve() / "gptbridge-automation" / _LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + max(0.0, wait_s)
    while True:
        try:
            with ProcessFileLock(lock_path):
                yield
            return
        except LockBusyError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def mandatory_test_gate(
    root: str | Path,
    *,
    runner: Runner | None = None,
    builder: Builder | None = None,
    config: Mapping[str, Any] | None = None,
    lock: Any | None = None,
) -> dict[str, Any]:
    """Run the native test suite against ``root`` (bounded, fail-closed).

    Returns a gate record: ``passed`` is False on any test failure,
    suite crash/timeout, stale-or-missing binaries that cannot be rebuilt,
    build failure, or config error — the push path treats every non-pass
    as a denial.
    """
    cfg = dict(config) if config is not None else push_gate_config()
    gate: dict[str, Any] = {
        "gate": "mandatory-tests",
        "harness": "native-test-suite/v1",
        "passed": False,
        "skipped": False,
        "suites": [],
        "totals": {"pass": 0, "fail": 0, "blocked": 0, "cases": 0},
        "failures": [],
        "rebuilt": False,
        "incomplete_evidence": False,
        "duration_ms": 0,
        "detail": "",
    }
    started = time.monotonic()

    def _done(detail: str) -> dict[str, Any]:
        gate["duration_ms"] = int((time.monotonic() - started) * 1000)
        gate["detail"] = detail
        return gate

    if cfg.get("config_error"):
        return _done(f"config-error:{cfg['config_error']}")
    if not cfg.get("enabled", True):
        gate["skipped"] = True
        return _done(
            "push_gate.enabled=false (governed config); "
            "mandatory-test PASS precondition unmet"
        )

    bin_dir = Path(root) / _BIN_DIR
    run = runner or _default_runner
    suite_timeout = float(cfg.get("suite_timeout_s") or DEFAULT_SUITE_TIMEOUT_S)
    run_budget = float(cfg.get("run_budget_s") or DEFAULT_RUN_BUDGET_S)
    lock_wait = float(cfg.get("lock_wait_s") or DEFAULT_LOCK_WAIT_S)

    try:
        with (lock if lock is not None else _gate_lock(root, lock_wait)):
            exes = _suite_exes(bin_dir)
            if _binaries_stale(Path(root), exes):
                if not cfg.get("auto_build", True):
                    return _done(
                        "test-binaries-stale-or-missing "
                        "(push_gate.auto_build=false)"
                    )
                try:
                    proc = _build_suites(
                        Path(root),
                        float(
                            cfg.get("build_timeout_s")
                            or DEFAULT_BUILD_TIMEOUT_S
                        ),
                        builder,
                    )
                except subprocess.TimeoutExpired:
                    return _done("build-timeout")
                except Exception as exc:  # noqa: BLE001 — fail closed
                    return _done(f"build-error:{type(exc).__name__}")
                exes = _suite_exes(bin_dir)
                if not exes or _binaries_stale(Path(root), exes):
                    rc = getattr(proc, "returncode", "?")
                    return _done(f"build-failed:rc={rc}")
                gate["rebuilt"] = True

            run_started = time.monotonic()
            # Keep on one line: check_bounded_worker_pools proves the bound
            # statically by reading the assignment RHS for a clamp token.
            max_parallel = max(1, min(MAX_PARALLEL_SUITES_CAP, int(cfg.get("max_parallel_suites") or DEFAULT_MAX_PARALLEL_SUITES)))
            gate["max_parallel_suites"] = max_parallel
            # Bounded-parallel orchestration: each suite writes a uniquely
            # named <stem>.json report and binds only ephemeral ports, so
            # worker threads cannot interleave report files or collide on
            # ports.  Results are merged on the caller thread in sorted exe
            # order so the gate record stays deterministic.
            results: dict[str, dict[str, Any]] = {}
            with ThreadPoolExecutor(
                max_workers=max_parallel,
                thread_name_prefix="push-gate-suite",
            ) as pool:
                pending = []
                for exe in exes:
                    if time.monotonic() - run_started > run_budget:
                        return _done(
                            f"run-budget-exceeded:{run_budget}s "
                            f"(submitted {len(pending)}/{len(exes)})"
                        )
                    pending.append(
                        (
                            exe,
                            pool.submit(
                                _execute_suite,
                                exe,
                                bin_dir,
                                suite_timeout,
                                run,
                            ),
                        )
                    )
                for exe, future in pending:
                    results[exe.name] = future.result()
            if time.monotonic() - run_started > run_budget:
                return _done(
                    f"run-budget-exceeded:{run_budget}s "
                    f"(ran {len(results)}/{len(exes)})"
                )
            for exe in exes:
                outcome = results[exe.name]
                gate["suites"].append(outcome["entry"])
                totals = gate["totals"]
                for case in outcome["cases"]:
                    if not isinstance(case, dict):
                        continue
                    status = str(case.get("status", "")).upper()
                    totals["cases"] += 1
                    if status == "PASS":
                        totals["pass"] += 1
                    elif status == "BLOCKED":
                        totals["blocked"] += 1
                    else:
                        totals["fail"] += 1
                        if len(gate["failures"]) < 8:
                            gate["failures"].append(
                                f"{case.get('suite', exe.stem)}/"
                                f"{case.get('name', '?')}: "
                                f"{str(case.get('detail', ''))[:120]}"
                            )
                rc = outcome["entry"]["returncode"]
                if rc is not None and rc not in (0, 1):
                    totals["fail"] += 1
                    gate["failures"].append(
                        f"{exe.stem}/suite_exit: rc={rc}"
                    )
    except LockBusyError:
        return _done(f"gate-busy:{_LOCK_FILENAME}")
    except Exception as exc:  # noqa: BLE001 — gate must total to a verdict
        return _done(f"gate-error:{type(exc).__name__}")

    totals = gate["totals"]
    gate["incomplete_evidence"] = totals["blocked"] > 0
    if totals["fail"] > 0:
        return _done(
            f"{totals['fail']} failed case(s): "
            + "; ".join(gate["failures"][:4])
        )
    if totals["cases"] == 0:
        return _done("no-test-cases-executed")
    gate["passed"] = True
    suffix = (
        f" ({totals['blocked']} blocked — incomplete evidence)"
        if totals["blocked"]
        else ""
    )
    return _done(
        f"{totals['pass']} pass / {totals['blocked']} blocked "
        f"across {len(gate['suites'])} suites{suffix}"
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

    One record carries the mandatory native-test gate outcome plus the
    revisions involved, so a push is provably preceded by audit PASS +
    native suite PASS.
    """
    from . import audit_log

    repo = GitRepository(root)
    local_sha = (
        repo.run(["rev-parse", branch_policy.MAIN_BRANCH]).stdout or ""
    ).strip()
    origin_sha = (
        repo.run(["rev-parse", f"origin/{branch_policy.MAIN_BRANCH}"]).stdout
        or ""
    ).strip()
    gate_summary = "none"
    if test_gate is not None:
        totals = (
            test_gate.get("totals")
            if isinstance(test_gate.get("totals"), Mapping)
            else {}
        )
        gate_summary = (
            f"tests={'pass' if test_gate.get('passed') else 'fail'}"
            f" skipped={bool(test_gate.get('skipped'))}"
            f" suites={len(test_gate.get('suites') or [])}"
            f" pass={totals.get('pass', 0)}"
            f" fail={totals.get('fail', 0)}"
            f" blocked={totals.get('blocked', 0)}"
            f" rebuilt={bool(test_gate.get('rebuilt'))}"
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
    "DEFAULT_BUILD_TIMEOUT_S",
    "DEFAULT_LOCK_WAIT_S",
    "DEFAULT_MAX_PARALLEL_SUITES",
    "DEFAULT_RUN_BUDGET_S",
    "DEFAULT_SUITE_TIMEOUT_S",
    "MAX_PARALLEL_SUITES_CAP",
    "mandatory_test_gate",
    "push_gate_config",
    "record_convergence_evidence",
    "record_push_evidence",
]
