#!/usr/bin/env python
"""P1-9 evidence: governed <=5s start/stop SLA measurement for the 7 toolbox tools.

Same launcher/worker split as ``scripts/toolbox-soak.py``: the launcher mints
``GPTBRIDGE_GOVERNANCE_BOOTSTRAP`` (identity attestation bound to this PID)
and spawns the worker as a child, so the worker's ``ToolboxService`` passes
the launcher-ancestor contract exactly like the real Electron/boot-core
spawn of ``main.py``.

Worker mode drives the real governed paths:
  * ``start_tool(payload, background=True)`` -> measure until the request
    resolves AND a ``running`` row exists in the shared process registry;
  * ``force_close_tool(payload)`` -> measure until the request resolves AND
    no ``running`` row remains.

The 7 tools are the toolbox-visible independent tools
(``main_system_independent_tool=True``, not ``hidden_from_toolbox``):
ai-assistant, ai-collaboration, file-sorter, investment-mobile, local-model,
model-dialogue, vaultly.  ``system-rescue`` is hidden from the toolbox and
``star-chat`` is not an independent tool; ``xingcheng`` is the runtime
identity of ``local-model``.

Evidence JSON defaults to
``governance_rule/execution/audit/convergence/p6-tool-startstop-sla-<date>.json``.
Exits 0 when every tool starts and stops within ``--sla`` seconds.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Reuse the soak harness helpers (bootstrap token mint + governed service).
sys.path.insert(0, str(Path(__file__).resolve().parent))
_soak = importlib.import_module("toolbox-soak")

TOOLS = [
    "ai-assistant",
    "ai-collaboration",
    "file-sorter",
    "investment-mobile",
    "local-model",
    "model-dialogue",
    "vaultly",
]
REGISTRY_PATH = ROOT / "main-system" / "runtime" / "state" / "process-registry.json"
CONVERGENCE_DIR = ROOT / "governance_rule" / "execution" / "audit" / "convergence"


def _running_modules() -> set[str]:
    try:
        registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    except Exception:
        return set()
    return {
        str(row.get("module_id") or "")
        for row in registry.get("processes", [])
        if row.get("health") == "running"
    }


async def _wait_state(tool_id: str, want_running: bool, timeout: float) -> float:
    """Poll the shared registry until ``tool_id`` running-state matches.

    Returns elapsed seconds (== ``timeout`` when the deadline was hit).
    """
    start = time.monotonic()
    while True:
        if (tool_id in _running_modules()) == want_running:
            return time.monotonic() - start
        if time.monotonic() - start >= timeout:
            return time.monotonic() - start
        await asyncio.sleep(0.05)


async def _measure(args: argparse.Namespace) -> dict:
    service = _soak._build_service()
    try:
        await asyncio.to_thread(service.reconcile_process_registry)
    except Exception:
        pass
    results: list[dict] = []
    for tool_id in TOOLS:
        request_id = f"p6-sla-{tool_id}-{time.time_ns()}"

        t0 = time.monotonic()
        start_result = await service.start_tool(
            {"tool_id": tool_id, "background": True, "request_id": request_id}
        )
        start_s = time.monotonic() - t0
        start_s += await _wait_state(tool_id, True, args.sla)

        t1 = time.monotonic()
        stop_result = await service.force_close_tool(
            {"tool_id": tool_id, "request_id": request_id}
        )
        stop_s = time.monotonic() - t1
        stop_s += await _wait_state(tool_id, False, args.sla)

        within = (
            bool(start_result.get("ok"))
            and bool(stop_result.get("ok"))
            and start_s <= args.sla
            and stop_s <= args.sla
        )
        results.append(
            {
                "tool_id": tool_id,
                "start_s": round(start_s, 3),
                "stop_s": round(stop_s, 3),
                "start_ok": bool(start_result.get("ok")),
                "stop_ok": bool(stop_result.get("ok")),
                "start_error": start_result.get("error_code"),
                "stop_error": stop_result.get("error_code"),
                "within_sla": within,
            }
        )
    return {
        "evidence": "7-tool governed start/stop <=5s SLA (P1-9)",
        "schema": "star-tool-sla/v1",
        "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "sla_seconds": args.sla,
        "tools": results,
        "ok": all(r["within_sla"] for r in results),
    }


def _spawn_worker(args: argparse.Namespace) -> int:
    """Mint the launcher bootstrap token, spawn the worker as a child."""
    import os
    import subprocess

    from core_system.governance_runtime import GOVERNANCE_BOOTSTRAP_ENV

    env = dict(os.environ)
    env[GOVERNANCE_BOOTSTRAP_ENV] = _soak._issue_governance_bootstrap()
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--worker",
        "--sla",
        str(args.sla),
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
    parser.add_argument("--sla", type=float, default=5.0)
    parser.add_argument("--report", default=None)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    if not args.worker:
        return _spawn_worker(args)

    report = asyncio.run(_measure(args))
    if args.report:
        out = Path(args.report)
    else:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
        out = CONVERGENCE_DIR / f"p6-tool-startstop-sla-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    worst_start = max(r["start_s"] for r in report["tools"])
    worst_stop = max(r["stop_s"] for r in report["tools"])
    print(
        f"tool-sla ok={report['ok']} worst_start={worst_start}s "
        f"worst_stop={worst_stop}s report={out}"
    )
    for row in report["tools"]:
        print(
            f"  {row['tool_id']:<20} start={row['start_s']}s "
            f"stop={row['stop_s']}s ok={row['within_sla']}"
        )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
