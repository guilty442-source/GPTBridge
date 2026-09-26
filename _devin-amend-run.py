# -*- coding: utf-8 -*-
"""Standalone governed codex-amendment runner (worker:devin-cli).

Replicates the ``codex-amendment-intake`` governed path without the full
platform: launcher mints GPTBRIDGE_GOVERNANCE_BOOTSTRAP (attestation bound to
launcher PID) then spawns ``--worker``; the worker builds ToolboxService
through MainSystemGovernance.from_environment + PermissionSovereign, wakes
``local-model`` via the governed start_tool path, and runs
``advance_request`` on a worker thread while the sync search callable bridges
channel roundtrips back to the main loop — identical to the intake driver.

Usage:
    python _devin-amend-run.py <request1.json> [request2.json ...]
    python _devin-amend-run.py --probe-only   # warm search path only
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
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

OWNER_TOOL_ID = "local-model"
CHANNEL_TOOL_ID = "xingcheng"
WEB_SEARCH_COMMAND = "xingcheng_web_search"
WEB_SEARCH_TIMEOUT_S = 30.0


def _issue_governance_bootstrap() -> str:
    """Mint the launcher bootstrap token exactly as toolbox-soak does."""
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


def _build_service():
    from core_system.governance_runtime import MainSystemGovernance
    from governance import PermissionSovereign
    from tasks.toolbox_service import ToolboxService

    governance = MainSystemGovernance.from_environment(ROOT)
    sovereign = PermissionSovereign(None, governance=governance)
    service = ToolboxService(
        ROOT, governance=governance, permission_sovereign=sovereign
    )
    return service, sovereign


async def _search_roundtrip(service, sovereign, query: str) -> dict:
    """Exact copy of CodexAmendmentIntakeDriver._search_roundtrip."""
    request_id = f"codex-audit-search-{time.time_ns()}"
    try:
        queued = await service.request_tool_execution(
            {
                "tool_id": OWNER_TOOL_ID,
                "request_id": request_id,
                "_governed_command": WEB_SEARCH_COMMAND,
                "query": query,
            }
        )
    except Exception as error:
        return {
            "ok": False,
            "source": "xingcheng-web-search",
            "query": query,
            "error": f"queue:{type(error).__name__}",
        }
    if not isinstance(queued, dict) or queued.get("ok") is not True:
        return {
            "ok": False,
            "source": "xingcheng-web-search",
            "query": query,
            "error": "queue-denied",
        }
    deadline = time.monotonic() + WEB_SEARCH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            response = await asyncio.to_thread(
                sovereign.tool_execution_response,
                CHANNEL_TOOL_ID,
                request_id,
            )
        except Exception:
            response = None
        if isinstance(response, dict) and str(
            response.get("status") or ""
        ) in {"completed", "failed", "cancelled"}:
            body = response.get("response")
            if response.get("status") == "completed" and isinstance(body, dict):
                body.setdefault("source", "xingcheng-web-search")
                return body
            return {
                "ok": False,
                "source": "xingcheng-web-search",
                "query": query,
                "error": f"tool-{response.get('status')}",
            }
        await asyncio.sleep(0.5)
    return {
        "ok": False,
        "source": "xingcheng-web-search",
        "query": query,
        "error": "timeout",
    }


async def _worker(paths: list[str]) -> int:
    service, sovereign = _build_service()
    loop = asyncio.get_running_loop()

    def channel_search(query: str) -> dict:
        future = asyncio.run_coroutine_threadsafe(
            _search_roundtrip(service, sovereign, query), loop
        )
        try:
            return future.result(timeout=WEB_SEARCH_TIMEOUT_S + 10.0)
        except Exception as error:
            return {
                "ok": False,
                "source": "xingcheng-web-search",
                "query": query,
                "error": f"roundtrip:{type(error).__name__}",
            }

    try:
        active = False
        try:
            active = bool(
                await service.tool_process_active(OWNER_TOOL_ID)
            )
        except Exception:
            active = False
        if not active:
            start_result = await service.start_tool(
                {
                    "tool_id": OWNER_TOOL_ID,
                    "request_id": f"codex-audit-wake-{time.time_ns()}",
                    "background": True,
                }
            )
            print("start_tool:", json.dumps(start_result, ensure_ascii=False)[:600], flush=True)
            for _ in range(8):
                await asyncio.sleep(2)
                try:
                    if await service.tool_process_active(OWNER_TOOL_ID):
                        active = True
                        break
                except Exception:
                    pass
        if not active:
            print("local-model failed to become active", flush=True)
            return 2
        print("local-model active", flush=True)

        # Warm the xingcheng_web_search path before the audit: the
        # five-sovereign gate carries a hard 30 s flow deadline and a cold
        # first roundtrip (searchd spin-up inside local-model) blows it.
        t0 = time.monotonic()
        warm = await _search_roundtrip(service, sovereign, "codex-audit-warmup")
        print("warmup search:", round(time.monotonic() - t0, 1), "s ->",
              json.dumps(warm, ensure_ascii=False)[:400], flush=True)

        if "--probe-only" in paths:
            return 0
        paths = [p for p in paths if not p.startswith("--")]

        from governance_rule.execution.codex_amendment_driver import (
            advance_request,
        )
        from governance_rule.execution.codex_amendment_lifecycle import (
            CodexAmendmentRequestLedger,
        )

        ledger = CodexAmendmentRequestLedger()
        any_fail = False
        for path in paths:
            outcome = await asyncio.to_thread(
                asyncio.run,
                advance_request(
                    path,
                    ledger=ledger,
                    search=channel_search,
                    auto_execute=True,
                ),
            )
            print("=== outcome ===", flush=True)
            print(json.dumps(outcome, ensure_ascii=False, indent=2)[:4000], flush=True)
            if not outcome.get("ok"):
                any_fail = True
        return 1 if any_fail else 0
    finally:
        try:
            await service.shutdown_managed_tools()
        except Exception:
            pass


def main() -> int:
    args = sys.argv[1:]
    if "--worker" in args:
        paths = [a for a in args if a != "--worker"]
        return asyncio.run(_worker(paths))
    env = os.environ.copy()
    env["GPTBRIDGE_GOVERNANCE_BOOTSTRAP"] = _issue_governance_bootstrap()
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", *args],
        env=env,
    )
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
