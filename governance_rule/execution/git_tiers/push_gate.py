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
  suite's own environmental-abstain verdict — every BLOCKED case is
  classified through ``native/test_suites/suite_criticality.json`` (G99)
  into ``blocked_suite`` / ``blocked_reason`` / ``required_evidence`` /
  ``affected_capability`` / ``release_impact`` / ``criticality``; a
  BLOCKED case on a ``release-critical`` suite (the default for
  unregistered suites) denies the push, only suites explicitly
  registered ``experimental`` keep the non-blocking
  ``incomplete_evidence`` semantics (INCOMPLETE_EVIDENCE ≠ FAIL).

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
import time
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
_SUITE_MANIFEST_NAME = "suite-manifest.json"

# G97: the C# TestSuiteOrchestrator is the SOLE native suite orchestrator
# (§10.60.1).  The gate resolves the built executable (Release first),
# rebuilds it from source when missing/stale, and consumes its typed
# orchestration report instead of spawning suite processes itself.
_ORCH_DIR = _NATIVE_TEST_DIR / "csharp"
_ORCH_PROJECT = _ORCH_DIR / "TestSuiteOrchestrator.csproj"
_ORCH_SOURCE = _ORCH_DIR / "Program.cs"
_ORCH_EXE_RELEASE = (
    _ORCH_DIR / "bin" / "Release" / "net10.0" / "TestSuiteOrchestrator.exe"
)
_ORCH_EXE_DEBUG = (
    _ORCH_DIR / "bin" / "Debug" / "net10.0" / "TestSuiteOrchestrator.exe"
)
_ORCH_REPORT_NAME = "native-orchestration-report.json"

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
DEFAULT_ORCH_BUILD_TIMEOUT_S = 120.0
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


def _orchestrator_exe(root: Path) -> Path | None:
    """The built C# orchestrator executable (Release preferred)."""
    for rel in (_ORCH_EXE_RELEASE, _ORCH_EXE_DEBUG):
        exe = root / rel
        if exe.is_file():
            return exe
    return None


def _orchestrator_stale(root: Path, exe: Path) -> bool:
    """Rebuild when the orchestrator sources are newer than the exe."""
    try:
        exe_mtime = exe.stat().st_mtime
        for rel in (_ORCH_SOURCE, _ORCH_PROJECT):
            src = root / rel
            if src.is_file() and src.stat().st_mtime > exe_mtime:
                return True
    except OSError:
        return True
    return False


def _ensure_orchestrator(
    root: Path, timeout_s: float
) -> Path | None:
    """Resolve the orchestrator exe, rebuilding via ``dotnet build`` when
    missing or stale.  Returns ``None`` when unavailable — fail closed."""
    exe = _orchestrator_exe(root)
    if exe is not None and not _orchestrator_stale(root, exe):
        return exe
    try:
        proc = subprocess.run(
            [
                "dotnet", "build", str(root / _ORCH_PROJECT),
                "-c", "Release", "--nologo", "-v", "q",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _orchestrator_exe(root)


def _default_orchestrator(argv: list[str], cwd: Path, timeout_s: float) -> Any:
    """Invoke the C# orchestrator.  Output goes to a log file, not a pipe:
    suite processes may spawn grandchildren that inherit a pipe and keep
    it open past the parent's exit — a captured pipe could then never
    reach EOF and would hang the gate past its budget."""
    log_path = Path(cwd) / "_gate_orchestrator.log"
    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        return subprocess.run(
            argv,
            cwd=cwd,
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _orchestration_report(bin_dir: Path) -> dict[str, Any]:
    """Parse the orchestrator's ``native-orchestration-report.json``."""
    try:
        data = json.loads(
            (bin_dir / _ORCH_REPORT_NAME).read_text(encoding="utf-8-sig")
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _suite_manifest(bin_dir: Path) -> dict[str, Any]:
    """Parse ``bin/suite-manifest.json`` (emitted by build.ps1 with the
    built source revision).  Missing/malformed → ``{}``; callers treat an
    absent manifest as stale build output (G99 revision binding)."""
    try:
        data = json.loads(
            (bin_dir / _SUITE_MANIFEST_NAME).read_text(
                encoding="utf-8-sig"
            )
        )
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


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
    orchestrator: Runner | None = None,
    builder: Builder | None = None,
    config: Mapping[str, Any] | None = None,
    lock: Any | None = None,
) -> dict[str, Any]:
    """Run the native test suite against ``root`` (bounded, fail-closed).

    G97: suite execution is delegated to the C# ``TestSuiteOrchestrator``
    (the sole native orchestrator, §10.60.1) — the gate resolves/rebuilds
    it, invokes it once under ``run_budget_s``, and consumes its typed
    ``native-orchestration-report.json`` (per-suite results, blocked
    classification, revision binding, artifact hash and the mandated
    ``total_gate_time``/``process_startup_time``/``actual_test_time``/
    ``result_collection_time`` decomposition).  ``passed`` is False on
    any test failure, suite crash/timeout, missing/malformed report,
    orchestrator failure, stale binaries that cannot be rebuilt, build
    failure, or config error — every non-pass denies the push.
    """
    cfg = dict(config) if config is not None else push_gate_config()
    gate: dict[str, Any] = {
        "gate": "mandatory-tests",
        "harness": "native-test-suite/v1",
        "orchestrator": "native-test-orchestrator/v2",
        "passed": False,
        "skipped": False,
        "suites": [],
        "totals": {"pass": 0, "fail": 0, "blocked": 0, "cases": 0},
        "failures": [],
        "rebuilt": False,
        "incomplete_evidence": False,
        "blocked_classifications": [],
        "source_revision": None,
        "head_revision": None,
        "revision_match": None,
        "artifact_hash": None,
        "timing": {},
        "duration_ms": 0,
        "detail": "",
    }
    manifest: dict[str, Any] = {}
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

    root_path = Path(root)
    bin_dir = root_path / _BIN_DIR
    suite_timeout = float(cfg.get("suite_timeout_s") or DEFAULT_SUITE_TIMEOUT_S)
    run_budget = float(cfg.get("run_budget_s") or DEFAULT_RUN_BUDGET_S)
    lock_wait = float(cfg.get("lock_wait_s") or DEFAULT_LOCK_WAIT_S)

    try:
        with (lock if lock is not None else _gate_lock(root_path, lock_wait)):
            if orchestrator is None:
                # Resolve (or rebuild) the sole orchestrator before any
                # suite decision — without it no suite evidence can exist.
                exe = _ensure_orchestrator(
                    root_path,
                    float(
                        cfg.get("orch_build_timeout_s")
                        or DEFAULT_ORCH_BUILD_TIMEOUT_S
                    ),
                )
                if exe is None:
                    return _done(
                        "orchestrator-unavailable: TestSuiteOrchestrator "
                        "missing and `dotnet build` failed"
                    )
                run = lambda argv, cwd, timeout: _default_orchestrator(  # noqa: E731
                    [str(exe), *argv], cwd, timeout
                )
            else:
                run = orchestrator

            exes = _suite_exes(bin_dir)
            manifest = _suite_manifest(bin_dir)
            # G99: a missing/empty-revision suite manifest means the binaries
            # carry no bound source revision — same treatment as stale code
            # artifacts (rebuild when auto_build, deny otherwise).
            if _binaries_stale(root_path, exes) or not manifest.get(
                "revision"
            ):
                if not cfg.get("auto_build", True):
                    return _done(
                        "test-binaries-stale-or-missing "
                        "(push_gate.auto_build=false)"
                    )
                try:
                    proc = _build_suites(
                        root_path,
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
                manifest = _suite_manifest(bin_dir)
                if not exes or _binaries_stale(root_path, exes):
                    rc = getattr(proc, "returncode", "?")
                    return _done(f"build-failed:rc={rc}")
                if not manifest.get("revision"):
                    return _done("suite-manifest-missing")
                gate["rebuilt"] = True

            # Keep on one line: check_bounded_worker_pools proves the bound
            # statically by reading the assignment RHS for a clamp token.
            max_parallel = max(1, min(MAX_PARALLEL_SUITES_CAP, int(cfg.get("max_parallel_suites") or DEFAULT_MAX_PARALLEL_SUITES)))
            gate["max_parallel_suites"] = max_parallel

            # One orchestrated run: the C# orchestrator owns bounded
            # concurrency, per-suite timeout, process cleanup, report
            # consolidation, artifact hash and revision verification.
            try:
                proc = run(
                    [
                        "--run",
                        "--bin", str(bin_dir),
                        "--max-parallel", str(max_parallel),
                        "--suite-timeout-s", str(int(suite_timeout)),
                        "--require-manifest",
                    ],
                    bin_dir,
                    run_budget,
                )
            except subprocess.TimeoutExpired:
                return _done(
                    f"run-budget-exceeded:{run_budget}s (orchestrator)"
                )
            except Exception as exc:  # noqa: BLE001 — fail closed
                return _done(
                    f"orchestrator-error:{type(exc).__name__}"
                )
            report = _orchestration_report(bin_dir)
            if not report:
                rc = getattr(proc, "returncode", "?")
                return _done(
                    f"orchestration-report-missing:rc={rc}"
                )
            gate["suites"] = [
                dict(s) for s in report.get("suites") or []
                if isinstance(s, Mapping)
            ]
            gate["totals"] = {
                "pass": int(report.get("passed", 0) or 0),
                "fail": int(report.get("failed", 0) or 0),
                "blocked": int(report.get("blocked", 0) or 0),
                "cases": int(report.get("cases", 0) or 0),
            }
            gate["failures"] = [
                str(f) for f in report.get("failures") or []
            ][:8]
            gate["blocked_classifications"] = [
                dict(c)
                for c in report.get("blocked_classifications") or []
                if isinstance(c, Mapping)
            ]
            gate["artifact_hash"] = report.get("artifact_hash")
            gate["timing"] = (
                dict(report["timing"])
                if isinstance(report.get("timing"), Mapping)
                else {}
            )
            verdict = str(report.get("verdict", "FAIL")).upper()
    except LockBusyError:
        return _done(f"gate-busy:{_LOCK_FILENAME}")
    except Exception as exc:  # noqa: BLE001 — gate must total to a verdict
        return _done(f"gate-error:{type(exc).__name__}")

    # G99: bind the gate record to the manifest's source revision and the
    # current HEAD so suite evidence can be tied to one revision/generation.
    gate["source_revision"] = manifest.get("revision")
    try:
        head = GitRepository(root_path).run(["rev-parse", "HEAD"])
        gate["head_revision"] = (
            (head.stdout or "").strip() if head.returncode == 0 else None
        )
    except Exception:  # noqa: BLE001 — evidence only, never raises
        gate["head_revision"] = None
    if gate["source_revision"] and gate["head_revision"]:
        gate["revision_match"] = (
            gate["source_revision"] == gate["head_revision"]
        )

    totals = gate["totals"]
    gate["incomplete_evidence"] = totals["blocked"] > 0
    if totals["fail"] > 0:
        return _done(
            f"{totals['fail']} failed case(s): "
            + "; ".join(gate["failures"][:4])
        )
    if totals["cases"] == 0:
        return _done("no-test-cases-executed")
    if report.get("blocked_unclassified"):
        return _done(
            "blocked-unclassified: suite_criticality.json "
            "missing/malformed"
        )
    critical = [
        c
        for c in gate["blocked_classifications"]
        if c.get("criticality") != "experimental"
    ]
    if critical:
        first = critical[0]
        return _done(
            "blocked-release-critical: "
            f"{first['blocked_suite']}/{first['blocked_case']} "
            f"(+{len(critical) - 1} more) — "
            f"{first['affected_capability']} unverified"
        )
    if verdict not in ("PASS", "INCOMPLETE_EVIDENCE"):
        # Orchestrator denied for a reason the gate did not classify
        # above — still fail closed, never promote an unknown verdict.
        return _done(f"orchestrator-verdict-{verdict.lower()}")
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
