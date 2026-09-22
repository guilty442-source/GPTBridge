#!/usr/bin/env python3
"""§3.5-B / §10.66 sleep & wake measurement harness.

Measures the governed sleep/wake contract against the **live** backend
through the real governed paths:

- cold wake latency: ``toolbox_start_tool`` over IPC (target <= 5 s, A540);
- cold sleep latency: ``toolbox_stop_tool`` over IPC;
- warm wake: xingcheng native engine reload after AutoReleaseManager
  eviction (target <= 2 s) — ``--engine-warm``;
- no-thrash: ``SleepPolicyManager`` driven *in this harness* (the test
  environment) against an IPC-forwarding toolbox adapter.  The policy
  scans the unit under test, records every tier transition, and must
  never oscillate (hot<->warm flapping or repeated cold-sleep attempts).

The manager code path is the real one (``tasks.sleep_policy``); only the
toolbox is adapted so the governed start/stop executes on the live
backend — identical to the ModelServiceActivationBroker path.

Usage:
    python scripts/sleep-wake-measure.py --tool file-sorter \
        --cycles 3 --observe-minutes 30 [--engine-warm]

Evidence: ``main-system/runtime/logs/sleep-wake-measure-<utc>.json``.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
for _p in ("main-system/src-core", "main-system", "shared-layer/src", ""):
    sys.path.insert(0, str(ROOT / _p) if _p else str(ROOT))

DEFAULT_PORT = 8765
TOKEN_FILES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "GPTBridge" / "ipc"
    / "session-token",
    ROOT / "main-system" / "runtime" / "ipc" / "session-token",
]
LOG_DIR = ROOT / "main-system" / "runtime" / "logs"


def _workspace_instance() -> str:
    normalized = os.path.normcase(str(ROOT.absolute())).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _session_token() -> str:
    env = str(os.environ.get("GPTBRIDGE_IPC_SESSION_TOKEN") or "").strip()
    if env:
        return env.lower()
    for path in TOKEN_FILES:
        try:
            token = path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if token:
            return token
    return ""


class _IpcToolbox:
    """ToolboxService-compatible adapter forwarding to the live backend.

    ``SleepPolicyManager`` reads ``_active_request_by_tool`` /
    ``_started_request_by_tool`` (refreshed from ``toolbox_list_tools``)
    and calls ``stop_tool`` — which this adapter sends over the governed
    IPC command channel.
    """

    def __init__(self, port: int, token: str, instance: str) -> None:
        self._port = port
        self._token = token
        self._instance = instance
        self._active_request_by_tool: dict[str, str] = {}
        self._started_request_by_tool: dict[str, str] = {}

    def _url(self) -> str:
        expires = int(time.time()) + 30
        payload = f"{expires}.{secrets.token_hex(8)}.{self._instance}"
        sig = hmac.new(
            self._token.encode(), payload.encode(), hashlib.sha256
        ).hexdigest()
        return (
            f"ws://127.0.0.1:{self._port}/"
            f"?ticket={quote(payload + '.' + sig)}&instance={self._instance}"
        )

    async def _command(
        self, command: str, payload: dict[str, Any], timeout_s: float = 60.0
    ) -> dict[str, Any]:
        import websockets  # dev venv dependency

        async with websockets.connect(self._url()) as ws:
            await ws.send(json.dumps(
                {"command": "state_event_hello", "payload": {"cursor": None}}
            ))
            await ws.send(json.dumps({"command": command, "payload": payload}))
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.5, deadline - time.monotonic())
                    )
                except asyncio.TimeoutError:
                    break
                try:
                    event = json.loads(raw)
                except ValueError:
                    continue
                name = str(event.get("event") or "")
                if name == "heartbeat_ping":
                    await ws.send(json.dumps(
                        {"command": "heartbeat_pong", "payload": {}}
                    ))
                    continue
                if name == f"{command}_result":
                    return dict(event.get("payload") or {})
                if name == "error":
                    return {"ok": False, "error_code": "IPC_ERROR",
                            "message": str(event)}
            return {"ok": False, "error_code": "IPC_TIMEOUT",
                    "message": f"{command} produced no result in {timeout_s}s"}

    async def start_tool(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._command("toolbox_start_tool", payload, timeout_s=90.0)

    async def stop_tool(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._command("toolbox_stop_tool", payload, timeout_s=60.0)

    async def refresh_running(self) -> set[str]:
        result = await self._command("toolbox_list_tools", {}, timeout_s=30.0)
        running: set[str] = set()
        for tool in result.get("tools") or []:
            if str(tool.get("status") or "") == "running":
                running.add(str(tool.get("id") or ""))
        self._active_request_by_tool = {t: f"measure-{t}" for t in running}
        self._started_request_by_tool = dict(self._active_request_by_tool)
        return running


def _engine_warm_wake() -> dict[str, Any]:
    """Warm-tier wake: evict the native engine, measure reload latency."""
    sys.path.insert(
        0, str(ROOT / "Standalone tools" / "local-model" / "src" / "backend"
                 / "services")
    )
    from xingcheng.infrastructure.native_engine import (
        _engine_cache, native_engine_for,
    )
    from xingcheng.infrastructure.native_transformer.execution.auto_release import (
        get_manager,
    )

    result: dict[str, Any] = {"target_s": 2.0}
    try:
        engine = native_engine_for()
        out = engine.generate(prompt="你好", max_tokens=4)
        result["cold_generate_ok"] = bool(out.get("ok"))
        mgr = get_manager()
        mgr.idle = 1  # force immediate eviction on next check
        deadline = time.time() + 30
        while _engine_cache and time.time() < deadline:
            mgr._check()
            time.sleep(0.5)
        result["evicted"] = not _engine_cache
        if not result["evicted"]:
            result["error"] = "eviction-timeout"
            return result
        t0 = time.monotonic()
        engine = native_engine_for()
        out = engine.generate(prompt="你好", max_tokens=4)
        result["warm_wake_s"] = round(time.monotonic() - t0, 2)
        result["warm_generate_ok"] = bool(out.get("ok"))
        result["pass"] = bool(
            result["warm_generate_ok"] and result["warm_wake_s"] <= 2.0
        )
    except Exception as error:  # noqa: BLE001 — measurement, never raises
        result["error"] = f"{type(error).__name__}: {error}"
    return result


async def _amain(args: argparse.Namespace) -> dict[str, Any]:
    token = _session_token()
    if not token:
        return {"ok": False, "error": "no IPC session token"}
    instance = _workspace_instance()
    toolbox = _IpcToolbox(args.port, token, instance)

    from tasks.sleep_policy import SleepPolicyManager

    class _App:
        _shutting_down = False

    mgr = SleepPolicyManager(_App(), toolbox)
    report: dict[str, Any] = {
        "evidence": "§10.66 sleep/wake measurement (§3.5-B)",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "tool": args.tool,
        "port": args.port,
    }

    running = await toolbox.refresh_running()
    report["initial_running"] = sorted(running)

    # ---- Phase 1: cold wake / cold sleep cycles ----------------------
    cycles: list[dict[str, Any]] = []
    for i in range(args.cycles):
        row: dict[str, Any] = {"cycle": i}
        t0 = time.monotonic()
        stop = await toolbox.stop_tool(
            {"tool_id": args.tool, "request_id": f"sleep-measure-stop-{i}"}
        )
        row["ensure_cold_s"] = round(time.monotonic() - t0, 2)
        row["ensure_cold_ok"] = bool(stop.get("ok"))

        t0 = time.monotonic()
        start = await toolbox.start_tool({
            "tool_id": args.tool,
            "request_id": f"sleep-measure-wake-{i}",
            "background": True,
            "runtime_mode": "source",
        })
        row["cold_wake_s"] = round(time.monotonic() - t0, 2)
        row["cold_wake_ok"] = bool(start.get("ok"))
        row["cold_wake_error"] = start.get("error_code") or ""
        if row["cold_wake_ok"]:
            # readiness is implicit in ok=true on the governed path;
            # confirm the tool reports running.
            running_now = await toolbox.refresh_running()
            row["listed_running"] = args.tool in running_now
            t0 = time.monotonic()
            stop = await toolbox.stop_tool(
                {"tool_id": args.tool,
                 "request_id": f"sleep-measure-sleep-{i}"}
            )
            row["cold_sleep_s"] = round(time.monotonic() - t0, 2)
            row["cold_sleep_ok"] = bool(stop.get("ok"))
            row["cold_sleep_error"] = (
                stop.get("error_code") or stop.get("message") or ""
            )
        cycles.append(row)
    report["cold_cycles"] = cycles
    wakes = [c["cold_wake_s"] for c in cycles if c.get("cold_wake_ok")]
    report["cold_wake_max_s"] = max(wakes) if wakes else None
    report["cold_wake_target_s"] = 5.0
    report["cold_wake_pass"] = bool(wakes) and max(wakes) <= 5.0

    # ---- Phase 2: policy-driven tier cycle + thrash observation ------
    # never_sleep everything except the unit under test.
    all_running = await toolbox.refresh_running()
    never_sleep = sorted((all_running | {"main-system", "model-dialogue"})
                         - {args.tool})
    policy = {
        "enabled": True,
        "scan_interval_s": 10.0,
        "warm_after_s": float(args.warm_after),
        "cold_after_s": float(args.cold_after),
        "never_sleep": never_sleep,
        "units": {},
    }
    transitions: list[dict[str, Any]] = []
    stop_calls: list[float] = []
    orig_stop = toolbox.stop_tool

    async def _recording_stop(payload: dict[str, Any]) -> dict[str, Any]:
        stop_calls.append(time.time())
        return await orig_stop(payload)

    toolbox.stop_tool = _recording_stop  # type: ignore[method-assign]

    # Demand wake #2 — the unit must come back hot after cold sleep.
    t0 = time.monotonic()
    start = await toolbox.start_tool({
        "tool_id": args.tool,
        "request_id": "sleep-measure-policy-wake",
        "background": True,
        "runtime_mode": "source",
    })
    report["policy_wake_s"] = round(time.monotonic() - t0, 2)
    report["policy_wake_ok"] = bool(start.get("ok"))

    deadline = time.monotonic() + args.observe_minutes * 60
    prev_decision: str | None = None
    prev_tier: str | None = None
    scan_i = 0
    while time.monotonic() < deadline:
        scan_i += 1
        await toolbox.refresh_running()
        try:
            await mgr._scan(policy)
        except Exception as error:  # noqa: BLE001 — record, keep scanning
            transitions.append({"scan": scan_i,
                                "error": f"{type(error).__name__}: {error}"})
        tier = mgr._tiers.get(args.tool)
        decision = mgr._last_decisions.get(args.tool)
        if tier != prev_tier or decision != prev_decision:
            transitions.append({
                "scan": scan_i, "at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "tier": tier, "decision": decision,
            })
            prev_tier, prev_decision = tier, decision
        await asyncio.sleep(float(policy["scan_interval_s"]))

    report["policy"] = {
        "warm_after_s": policy["warm_after_s"],
        "cold_after_s": policy["cold_after_s"],
        "scans": scan_i,
    }
    report["transitions"] = transitions
    report["stop_calls"] = len(stop_calls)
    report["tiers_final"] = dict(mgr._tiers)

    # ---- Phase 3: warm engine wake (optional) ------------------------
    if args.engine_warm:
        report["engine_warm"] = await asyncio.to_thread(_engine_warm_wake)

    # ---- Verdict ------------------------------------------------------
    tier_sequence = [t.get("tier") for t in transitions]
    thrash = len(tier_sequence) != len(set(tier_sequence + [None])) - 1 and \
        len(tier_sequence) > 3
    report["verdict"] = {
        "cold_wake_pass": report["cold_wake_pass"],
        "engine_warm_pass": (report.get("engine_warm") or {}).get("pass"),
        "no_thrash": len(tier_sequence) <= 3 and not thrash,
        "transitions_count": len(transitions),
        "note": (
            "policy-driven: expect hot -> warm -> cold-sleep once, then "
            "steady; repeated tier flapping = thrash"
        ),
    }
    report["ok"] = bool(report["cold_wake_pass"]) and report["verdict"]["no_thrash"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tool", default="file-sorter")
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--warm-after", type=float, default=45.0,
                        help="policy warm tier threshold (s)")
    parser.add_argument("--cold-after", type=float, default=120.0,
                        help="policy cold sleep threshold (s)")
    parser.add_argument("--observe-minutes", type=float, default=30.0)
    parser.add_argument("--engine-warm", action="store_true")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(name)s %(levelname)s %(message)s",
    )

    report = asyncio.run(_amain(args))
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    out = Path(args.out) if args.out else (
        LOG_DIR / f"sleep-wake-measure-{stamp}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report.get("verdict", {}), ensure_ascii=False))
    print(f"ok={report.get('ok')} report={out}")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
