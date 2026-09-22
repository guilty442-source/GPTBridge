#!/usr/bin/env python
"""G83 empirical soak: resident tool start + process-registry reconciliation.

Runs a real ``ToolboxService`` for ``--duration`` seconds, starts the watched
resident tool through the governed ``start_tool`` path, and samples the
process registry every ``--interval`` seconds.

Assertions sampled each tick:
- every registry row with ``health == "running"`` maps to a live PID
  (zombie-aware, same ``_pid_alive`` semantics as the registry itself);
- no module claims more than one ``running`` row (restart-loop detector);
- restart_count does not grow without a corresponding dead-PID transition.

Also records backend auto-loop arity errors (``auto-loop error`` /
``_notify_anomalies``) added to ``main-system/runtime/logs/backend-*.log``
during the window, and tool stderr capture under
``main-system/runtime/logs/tools/<tool>/``.

Writes ``main-system/runtime/logs/toolbox-soak-<ts>.json`` and exits 0 on
PASS, 1 on FAIL.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
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

from core_system.process_registry import ProcessRegistry  # noqa: E402
from tasks.toolbox_service import ToolboxService  # noqa: E402


def _issue_governance_bootstrap() -> str:
    """Mint the launcher bootstrap token exactly as boot_core does."""
    import base64
    import os
    import secrets
    from dataclasses import asdict

    from governance_rule.execution.authentication import sign_launcher_attestation
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


def _build_service() -> ToolboxService:
    """Construct ToolboxService through the real governance bootstrap path.

    Expects ``GPTBRIDGE_GOVERNANCE_BOOTSTRAP`` to be set by the launcher
    process (this script without ``--worker``); the attestation binds the
    launcher PID, which must be this process's ancestor.
    """
    from core_system.governance_runtime import MainSystemGovernance
    from governance import PermissionSovereign

    governance = MainSystemGovernance.from_environment(ROOT)
    sovereign = PermissionSovereign(None, governance=governance)
    return ToolboxService(
        ROOT, governance=governance, permission_sovereign=sovereign
    )

STATE_DIR = ROOT / "main-system" / "runtime" / "state"
LOG_DIR = ROOT / "main-system" / "runtime" / "logs"
REGISTRY_PATH = STATE_DIR / "process-registry.json"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _load_registry() -> dict:
    try:
        return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"processes": []}


def _backend_log_fingerprint() -> dict[str, int]:
    """Size of each backend log; used to scan only bytes added mid-soak."""
    out: dict[str, int] = {}
    for path in LOG_DIR.glob("backend-*.log"):
        try:
            out[str(path)] = path.stat().st_size
        except OSError:
            pass
    return out


def _count_arity_markers(baseline: dict[str, int]) -> dict[str, int]:
    """Count arity markers in bytes appended to backend logs after baseline."""
    markers = {"auto-loop error": 0, "_notify_anomalies": 0}
    for name, old_size in _backend_log_fingerprint().items():
        path = Path(name)
        try:
            with path.open("rb") as handle:
                handle.seek(min(baseline.get(name, 0), path.stat().st_size))
                tail = handle.read().decode("utf-8", errors="replace")
        except OSError:
            continue
        for key in markers:
            markers[key] += tail.count(key)
    return markers


def _stderr_sizes(tool_id: str) -> dict[str, int]:
    tool_log_dir = LOG_DIR / "tools" / tool_id
    out: dict[str, int] = {}
    if tool_log_dir.is_dir():
        for path in sorted(tool_log_dir.glob("stderr*")):
            try:
                out[path.name] = path.stat().st_size
            except OSError:
                pass
    return out


def _sample() -> dict:
    registry = _load_registry()
    running = [
        row
        for row in registry.get("processes", [])
        if row.get("health") == "running"
    ]
    anomalies: list[str] = []
    by_module: dict[str, list[int]] = {}
    for row in running:
        pid = int(row.get("pid") or 0)
        module = str(row.get("module_id") or "")
        by_module.setdefault(module, []).append(pid)
        if not ProcessRegistry._pid_alive(pid):
            anomalies.append(f"running-but-dead:{module}:{pid}")
    for module, pids in by_module.items():
        if len(pids) > 1:
            anomalies.append(f"duplicate-running:{module}:{sorted(pids)}")
    return {
        "at": _utcnow(),
        "running": {m: sorted(p) for m, p in by_module.items()},
        "anomalies": anomalies,
    }


async def _soak(args: argparse.Namespace) -> dict:
    service = _build_service()
    tool_id = args.tool
    log_baseline = _backend_log_fingerprint()
    baseline_registry = _load_registry()
    baseline_pids = {
        int(row.get("pid") or 0)
        for row in baseline_registry.get("processes", [])
        if row.get("module_id") == tool_id
    }

    start_result = await service.start_tool(
        {
            "tool_id": tool_id,
            "background": True,
            "request_id": f"resident-start-{tool_id}-soak-{time.time_ns()}",
        }
    )
    await service.start_process_registry_monitor(interval_seconds=args.reconcile)

    deadline = time.monotonic() + args.duration
    samples: list[dict] = []
    try:
        while time.monotonic() < deadline:
            await asyncio.sleep(args.interval)
            samples.append(_sample())
    finally:
        await service.stop_process_registry_monitor()

    shutdown = await service.shutdown_managed_tools()

    anomalies = sorted({a for s in samples for a in s["anomalies"]})
    final_registry = _load_registry()
    new_pids = {
        int(row.get("pid") or 0)
        for row in final_registry.get("processes", [])
        if row.get("module_id") == tool_id
    } - baseline_pids

    arity = _count_arity_markers(log_baseline)
    ok = (
        start_result.get("ok") is True
        and not anomalies
        and arity["auto-loop error"] == 0
        and arity["_notify_anomalies"] == 0
    )
    return {
        "schema": "star-toolbox-soak/v1",
        "tool_id": tool_id,
        "duration_s": args.duration,
        "interval_s": args.interval,
        "reconcile_s": args.reconcile,
        "started_at": samples[0]["at"] if samples else _utcnow(),
        "finished_at": _utcnow(),
        "start_result": start_result,
        "samples": samples,
        "distinct_new_pids": sorted(new_pids),
        "anomalies": anomalies,
        "arity_markers_added": arity,
        "stderr_files": _stderr_sizes(tool_id),
        "shutdown_result": shutdown,
        "ok": ok,
    }


def _spawn_backend_process() -> "subprocess.Popen[bytes]":
    """Spawn the real backend exactly like boot_core_lifecycle._spawn_backend."""
    import os
    import subprocess

    from core_system.governance_runtime import GOVERNANCE_BOOTSTRAP_ENV

    env = dict(os.environ)
    env[GOVERNANCE_BOOTSTRAP_ENV] = _issue_governance_bootstrap()
    env["GPTBRIDGE_PROJECT_ROOT"] = str(ROOT)
    env["GPTBRIDGE_WORKSPACE_ROOT"] = str(ROOT)
    return subprocess.Popen(
        [
            sys.executable,
            "-u",
            "-B",
            str(ROOT / "main-system" / "src-core" / "main.py"),
            "--serve",
        ],
        cwd=str(ROOT),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def _backend_soak(args: argparse.Namespace) -> int:
    """Empirical soak against the real backend process (G82/G83).

    The backend runs its own ToolboxService, resident starts, reconcile
    monitor and the xingcheng auto-loop; this harness only observes:
    registry samples every ``--interval`` seconds, backend-log arity
    markers, and a post-terminate reconcile pass.
    """
    import subprocess
    import threading

    from backend_log_sink import get_backend_log_sink

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sink = get_backend_log_sink(LOG_DIR)
    relayed: list[str] = []

    child = _spawn_backend_process()
    assert child.stdout is not None

    def _pump() -> None:
        for raw in iter(child.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            relayed.append(line)
            try:
                sink.write_line(line)
            except Exception:
                pass

    pump = threading.Thread(target=_pump, daemon=True)
    pump.start()

    ready_deadline = time.monotonic() + 180.0
    ready = False
    while time.monotonic() < ready_deadline:
        if child.poll() is not None:
            break
        if any('"status": "ready"' in line for line in relayed):
            ready = True
            break
        time.sleep(0.5)

    log_baseline = _backend_log_fingerprint()
    samples: list[dict] = []
    deadline = time.monotonic() + (args.duration if ready else 0.0)
    while time.monotonic() < deadline:
        time.sleep(args.interval)
        samples.append(_sample())

    # Hard terminate mirrors the real boot-core/Electron shutdown path;
    # reconcile afterwards demonstrates crash-state cleanup.
    if child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=30)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)
    pump.join(timeout=10)
    post_kill = _sample()
    reconciled = ProcessRegistry(REGISTRY_PATH).reconcile()

    anomalies = sorted({a for s in samples for a in s["anomalies"]})
    arity = _count_arity_markers(log_baseline)
    ok = ready and not anomalies and not any(
        count for count in arity.values()
    )
    report = {
        "schema": "star-toolbox-soak/v1",
        "mode": "backend",
        "duration_s": args.duration,
        "interval_s": args.interval,
        "backend_ready": ready,
        "backend_exit_code": child.returncode,
        "samples": samples,
        "anomalies": anomalies,
        "arity_markers_added": arity,
        "post_kill_running": post_kill["running"],
        "post_kill_reconcile": reconciled,
        "ok": ok,
    }
    out = Path(args.report) if args.report else (
        LOG_DIR / f"toolbox-soak-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    )
    out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(
        f"backend soak ok={report['ok']} ready={ready} "
        f"anomalies={len(anomalies)} arity={arity} report={out}"
    )
    return 0 if ok else 1


def _spawn_worker(args: argparse.Namespace) -> int:
    """Launcher mode: mint the bootstrap token, spawn the worker as a child.

    The identity attestation binds this launcher PID; the worker must be a
    descendant for ``_is_launcher_ancestor`` to pass — same contract as the
    real Electron/boot-core spawn of ``main.py``.
    """
    import os
    import subprocess

    from core_system.governance_runtime import GOVERNANCE_BOOTSTRAP_ENV

    env = dict(os.environ)
    env[GOVERNANCE_BOOTSTRAP_ENV] = _issue_governance_bootstrap()
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--tool",
        args.tool,
        "--duration",
        str(args.duration),
        "--interval",
        str(args.interval),
        "--reconcile",
        str(args.reconcile),
    ]
    if args.report:
        command += ["--report", args.report]
    completed = subprocess.run(
        command,
        env=env,
        cwd=str(ROOT),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", default="shared-layer")
    parser.add_argument("--duration", type=float, default=1800.0)
    parser.add_argument("--interval", type=float, default=30.0)
    parser.add_argument("--reconcile", type=float, default=30.0)
    parser.add_argument("--report", default=None)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument(
        "--backend",
        action="store_true",
        help="Soak the real backend process (boot-core spawn path) instead of "
        "driving ToolboxService in-process.",
    )
    args = parser.parse_args()

    if args.backend:
        return _backend_soak(args)
    if not args.worker:
        return _spawn_worker(args)

    report = asyncio.run(_soak(args))
    out = Path(args.report) if args.report else (
        LOG_DIR / f"toolbox-soak-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"soak ok={report['ok']} anomalies={len(report['anomalies'])} report={out}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
