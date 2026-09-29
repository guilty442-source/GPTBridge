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

Module layout (A185 source-size split):

    push_gate_suites.py    native/test_suites layout + staleness + orchestrator
    push_gate_evidence.py  convergence + push-decision audit records
    push_gate.py           config, gate lock, mandatory_test_gate (this module)
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from .git_repository import GitRepository
from .process_lock import LockBusyError, ProcessFileLock
from .push_gate_evidence import (  # noqa: F401  (re-exported evidence surface)
    _STATE_FILENAME,
    _STATE_SCHEMA,
    _ahead_behind,
    _read_state,
    _state_path,
    _write_state,
    record_convergence_evidence,
    record_push_evidence,
)
from .push_gate_suites import (  # noqa: F401  (re-exported suite surface)
    _BIN_DIR,
    _BUILD_SCRIPT,
    _CODE_SUFFIXES,
    _DEP_ROOTS,
    _NATIVE_TEST_DIR,
    _ORCH_DIR,
    _ORCH_EXE_DEBUG,
    _ORCH_EXE_RELEASE,
    _ORCH_PROJECT,
    _ORCH_REPORT_NAME,
    _ORCH_SOURCE,
    _SUITE_GLOB,
    _SUITE_MANIFEST_NAME,
    _binaries_stale,
    _build_suites,
    _default_orchestrator,
    _ensure_orchestrator,
    _newest_source_mtime,
    _orchestration_report,
    _orchestrator_exe,
    _orchestrator_stale,
    _powershell,
    _suite_artifacts_stale,
    _suite_exes,
    _suite_manifest,
    Builder,
    Runner,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
_FLOWS_CONFIG = (
    PROJECT_ROOT / "main-system" / "config" / "automation-flows.json"
)
_LOCK_FILENAME = "push-test-gate.lock"

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
        "stale_artifacts": [],
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
            # artifacts (rebuild when auto_build, deny otherwise).  Exe
            # content hashes are checked against the manifest too — a
            # swapped or partially rebuilt binary is stale regardless of
            # timestamps.
            hash_stale = _suite_artifacts_stale(bin_dir, manifest)
            if (
                _binaries_stale(root_path, exes)
                or not manifest.get("revision")
                or hash_stale
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
                if (
                    not exes
                    or _binaries_stale(root_path, exes)
                    or _suite_artifacts_stale(bin_dir, manifest)
                ):
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
            gate["stale_artifacts"] = [
                str(a) for a in report.get("stale_artifacts") or []
            ]
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
