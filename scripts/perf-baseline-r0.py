#!/usr/bin/env python
"""§10.63 R0 baseline: real-backend resident-resource baseline (star-perf-baseline/v1).

Spawns the real backend through the governed bootstrap path (same as
``toolbox-soak.py --backend``), samples every GPTBridge python process's
RSS / CPU / threads every ``--interval`` seconds for ``--duration``
seconds, parses the startup-phase timings of this boot from the newest
``backend-*.log``, and persists a ``star-perf-baseline/v1`` snapshot via
``tasks.perf_baseline_snapshot``.

Acceptance targets (§1.1 / §10.63) recorded in the snapshot's ``targets``
field so before/after comparisons are direct: RSS <= 220 MB p95,
idle CPU <= 3%, ready <= 3.0 s, phase-5 <= 600 ms, <= 6 fixed-period jobs.

Writes ``main-system/runtime/state/perf-baseline-<ts>.json`` (plus the
latest pointer) and prints a summary. Exits 0 when the run completed;
the baseline itself carries no pass/fail (measurement only).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "main-system" / "src-core"
for extra in (
    CORE,
    ROOT / "main-system",
    ROOT / "governance_rule",
    ROOT / "shared-layer" / "src",
    ROOT,
):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from tasks.perf_baseline_snapshot import (  # noqa: E402
    collect_baseline,
    persist_baseline,
)

STATE_DIR = ROOT / "main-system" / "runtime" / "state"
LOG_DIR = ROOT / "main-system" / "runtime" / "logs"

# Fixed-period background work inventory (source constants, §10.63 measure
# 3 target: <= 6 entries). Each entry: name, nominal interval, source.
PERIODIC_JOBS = [
    {"name": "git commit sweep", "interval_s": 60.0,
     "source": "tasks/git_automation.py:41"},
    {"name": "git sync cycle", "interval_s": 300.0,
     "source": "tasks/git_automation.py:42"},
    {"name": "model service activation (idle)", "interval_s": 5.0,
     "source": "tasks/model_service_activation.py:45"},
    {"name": "model service activation (pending)", "interval_s": 1.0,
     "source": "tasks/model_service_activation.py:46"},
    {"name": "connection watchdog probe (backoff max)", "interval_s": 60.0,
     "source": "tasks/connection_watchdog.py:130"},
    {"name": "hot reload watcher poll (max)", "interval_s": 60.0,
     "source": "tasks/hot_reload_watcher.py:72"},
    {"name": "state outbox retry", "interval_s": 2.0,
     "source": "tasks/state_outbox_store.py:36"},
    {"name": "system automation coordinator", "interval_s": 60.0,
     "source": "core_system/system_automation_coordinator_constants.py:6"},
    {"name": "resource maintenance", "interval_s": 900.0,
     "source": "core_system/resource_maintenance.py:13"},
    {"name": "daily global cleaner", "interval_s": 86400.0,
     "source": "core_system/daily_global_cleaner_service.py:32"},
    {"name": "ipc heartbeat", "interval_s": 5.0,
     "source": "ipc/server_handler_helpers.py:32"},
    {"name": "maintenance retry", "interval_s": 60.0,
     "source": "core_system/maintenance_retry_policy.py:32"},
    {"name": "xingcheng self-maintenance", "interval_s": 300.0,
     "source": "xingcheng/application/service.py:173"},
    {"name": "auto-release check", "interval_s": 60.0,
     "source": "native_transformer/execution/auto_release.py:23"},
]

TARGETS = {
    "resident_rss_p95_mb": 220.0,
    "idle_cpu_pct": 3.0,
    "ready_s": 3.0,
    "phase5_ms": 600.0,
    "fixed_period_jobs": 6,
}

_PHASE_RE = re.compile(
    r'"(phase-[a-zA-Z0-9_-]+)"[^}]*?"duration_since_last_ms":\s*([0-9.]+)'
)


def _issue_governance_bootstrap() -> str:
    """Mint the launcher bootstrap token exactly as boot_core does."""
    from governance_rule.execution.authentication import (
        sign_launcher_attestation,
    )
    from governance_rule.execution.integrity import build_integrity_manifest

    workspace = str(ROOT)
    launcher_key = secrets.token_bytes(32)
    issued_at = int(time.time())
    key_id = secrets.token_hex(16)
    integrity = build_integrity_manifest(
        workspace, launcher_key, issued_at=issued_at, key_id=key_id
    )
    attestation = sign_launcher_attestation(
        launcher_key,
        actor="governance/main-system",
        bound_tool_id="main-system",
        caller_path="main-system/src-core/main.py",
        process_id=os.getpid(),
        issued_at=issued_at,
        key_id=key_id,
    )
    payload = {
        "format_version": 1,
        "launcher_key": base64.b64encode(launcher_key).decode("ascii"),
        "integrity_manifest": asdict(integrity),
        "identity_attestation": asdict(attestation),
    }
    return base64.b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")


def _spawn_backend() -> subprocess.Popen:
    from core_system.governance_runtime import GOVERNANCE_BOOTSTRAP_ENV

    env = dict(os.environ)
    env[GOVERNANCE_BOOTSTRAP_ENV] = _issue_governance_bootstrap()
    env["GPTBRIDGE_PROJECT_ROOT"] = str(ROOT)
    env["GPTBRIDGE_WORKSPACE_ROOT"] = str(ROOT)
    return subprocess.Popen(
        [sys.executable, "-u", "-B",
         str(ROOT / "main-system" / "src-core" / "main.py"), "--serve"],
        cwd=str(ROOT),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _gptbridge_python_procs() -> list:
    import psutil

    found = []
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            if "python" not in name or proc.info["pid"] == me:
                continue
            cmdline = " ".join(proc.info["cmdline"] or [])
            if str(ROOT).lower() in cmdline.lower():
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round((pct / 100.0) * (len(ordered) - 1))))
    return ordered[idx]


def _startup_phases(lines: list[str]) -> dict:
    """Parse phase timings from the spawned backend's relayed stdout."""
    phases: dict[str, float] = {}
    for line in lines:
        match = _PHASE_RE.search(line)
        if match:
            phases[match.group(1)] = float(match.group(2))
    return phases


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", type=float, default=1800.0,
                        help="sampling window seconds (default 1800 = 30 min)")
    parser.add_argument("--interval", type=float, default=10.0)
    parser.add_argument("--ready-timeout", type=float, default=180.0)
    args = parser.parse_args()

    import psutil

    spawned_at = time.time()
    child = _spawn_backend()
    relayed: list[str] = []

    def _pump() -> None:
        assert child.stdout is not None
        for raw in iter(child.stdout.readline, b""):
            relayed.append(raw.decode("utf-8", errors="replace").rstrip())

    pump = threading.Thread(target=_pump, daemon=True)
    pump.start()

    ready = False
    deadline = time.monotonic() + args.ready_timeout
    while time.monotonic() < deadline:
        if child.poll() is not None:
            break
        if any('"status": "ready"' in line for line in relayed):
            ready = True
            break
        time.sleep(0.5)
    ready_s = round(time.time() - spawned_at, 2) if ready else None
    if not ready:
        child.kill()
        print("[FAIL] backend did not reach ready within timeout")
        return 1
    print(f"[baseline] backend ready pid={child.pid} in {ready_s}s; "
          f"sampling {args.duration}s @ {args.interval}s")

    # Prime per-process cpu_percent counters, then sample.
    samples: dict[int, dict[str, list]] = {}
    procs = {p.pid: p for p in _gptbridge_python_procs()}
    for proc in procs.values():
        try:
            proc.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    end = time.monotonic() + args.duration
    tick = 0
    while time.monotonic() < end:
        time.sleep(args.interval)
        tick += 1
        for proc in _gptbridge_python_procs():
            try:
                with proc.oneshot():
                    cpu = proc.cpu_percent(None)
                    rss = proc.memory_info().rss / 1_048_576
                    threads = proc.num_threads()
                bucket = samples.setdefault(
                    proc.pid, {"rss_mb": [], "cpu_pct": [], "threads": []})
                bucket["rss_mb"].append(round(rss, 1))
                bucket["cpu_pct"].append(round(cpu, 2))
                bucket["threads"].append(threads)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if tick % 6 == 0:
            print(f"[baseline] tick {tick} ({tick * args.interval:.0f}s) "
                  f"procs={len(samples)}")

    # Per-process aggregates keyed by pid + cmdline tag.
    per_proc = {}
    for pid, series in samples.items():
        try:
            tag = " ".join(psutil.Process(pid).cmdline() or [])[:160]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            tag = "<exited>"
        lowered = tag.lower()
        if "main.py" in lowered and "--serve" in lowered:
            role = "backend"
        elif "scripts/" in lowered or "scripts\\" in lowered:
            role = "watcher"
        elif "perf-baseline-r0" in lowered:
            role = "sampler"
        else:
            role = "resident/tool"
        per_proc[pid] = {
            "role": role,
            "cmdline": tag,
            "samples": len(series["rss_mb"]),
            "rss_p50_mb": _percentile(series["rss_mb"], 50),
            "rss_p95_mb": _percentile(series["rss_mb"], 95),
            "rss_max_mb": max(series["rss_mb"], default=None),
            "cpu_mean_pct": round(
                sum(series["cpu_pct"]) / len(series["cpu_pct"]), 2)
            if series["cpu_pct"] else None,
            "cpu_p95_pct": _percentile(series["cpu_pct"], 95),
            "threads_max": max(series["threads"], default=None),
        }

    backend_rss = [
        v["rss_p95_mb"] for v in per_proc.values() if v["role"] == "backend"
    ]
    backend_cpu = [
        v["cpu_mean_pct"] for v in per_proc.values() if v["role"] == "backend"
    ]
    phases = _startup_phases(relayed)

    extra = {
        "mode": "r0-resident-baseline",
        "duration_s": args.duration,
        "interval_s": args.interval,
        "backend_pid": next(
            (k for k, v in per_proc.items() if v["role"] == "backend"),
            child.pid,
        ),
        "launcher_pid": child.pid,
        "backend_ready_s": ready_s,
        "startup_phases_ms": phases,
        "phase5_ms": phases.get("phase-5-classify-dependency-dag"),
        "processes": per_proc,
        "backend_rss_p95_mb": max(backend_rss, default=None),
        "backend_cpu_mean_pct": backend_cpu[0] if backend_cpu else None,
        "fixed_period_jobs": PERIODIC_JOBS,
        "fixed_period_job_count": len(PERIODIC_JOBS),
        "targets": TARGETS,
    }
    snapshot = collect_baseline(ROOT, extra=extra)
    path = persist_baseline(ROOT, snapshot)

    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
    pump.join(timeout=10)

    print(json.dumps({
        "report": str(path),
        "ready_s": ready_s,
        "phase5_ms": extra["phase5_ms"],
        "backend_rss_p95_mb": extra["backend_rss_p95_mb"],
        "backend_cpu_mean_pct": extra["backend_cpu_mean_pct"],
        "processes_sampled": len(per_proc),
        "fixed_period_jobs": len(PERIODIC_JOBS),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
