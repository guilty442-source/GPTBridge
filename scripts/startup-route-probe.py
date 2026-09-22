"""Startup 10s residual — UI / tool-route availability probe (live backend).

Measures the end-to-end route latencies a UI session exercises, against
the *running* backend (no restart, no state mutation — read-only
commands only):

  1. ticket-auth WebSocket handshake latency,
  2. ``state_event_hello`` → ``state_event_session`` round-trip,
  3. ``app:get-runtime-status`` command round-trip (UI status route),
  4. ``toolbox_list_tools`` command round-trip (tool route).

Writes ``runtime/logs/startup-route-probe-<ts>.json``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
PORT = int(os.environ.get("GPTBRIDGE_IPC_PORT", "8765"))
TOKEN_FILES = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "GPTBridge" / "ipc"
    / "session-token",
    ROOT / "main-system" / "runtime" / "ipc" / "session-token",
]
LOG_DIR = ROOT / "main-system" / "runtime" / "logs"


def _instance() -> str:
    normalized = os.path.normcase(str(ROOT.absolute())).replace("\\", "/")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:24]


def _token() -> str:
    env = str(os.environ.get("GPTBRIDGE_IPC_SESSION_TOKEN") or "").strip()
    if env:
        return env.lower()
    for path in TOKEN_FILES:
        try:
            tok = path.read_text(encoding="utf-8").strip().lower()
        except OSError:
            continue
        if tok:
            return tok
    return ""


def _url(token: str, instance: str) -> str:
    expires = int(time.time()) + 30
    payload = f"{expires}.{secrets.token_hex(8)}.{instance}"
    sig = hmac.new(token.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return (
        f"ws://127.0.0.1:{PORT}/"
        f"?ticket={quote(payload + '.' + sig)}&instance={instance}"
    )


async def _probe(token: str, instance: str, timeout_s: float = 15.0) -> dict:
    import websockets

    out: dict[str, object] = {}
    t0 = time.monotonic()
    async with websockets.connect(
        _url(token, instance), max_size=16 * 1024 * 1024
    ) as ws:
        out["auth_handshake_ms"] = round((time.monotonic() - t0) * 1000, 1)

        async def _command(cmd: str, payload: dict, expect: str) -> dict:
            t = time.monotonic()
            await ws.send(json.dumps({"command": cmd, "payload": payload}))
            deadline = time.monotonic() + timeout_s
            events: list[str] = []
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=max(0.5, deadline - time.monotonic())
                    )
                except asyncio.TimeoutError:
                    break
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                name = str(ev.get("event") or "")
                events.append(name)
                if name == "heartbeat_ping":
                    await ws.send(json.dumps(
                        {"command": "heartbeat_pong", "payload": {}}
                    ))
                    continue
                if name == expect or name == "error":
                    return {
                        "ms": round((time.monotonic() - t) * 1000, 1),
                        "events": events[:6],
                        "payload": ev.get("payload"),
                        "ok": name == expect,
                    }
            return {"ms": round((time.monotonic() - t) * 1000, 1),
                    "events": events[:6], "ok": False}

        out["session_hello"] = await _command(
            "state_event_hello", {"cursor": None}, "state_event_session"
        )
        out["runtime_status"] = await _command(
            "app:get-runtime-status", {}, "app:get-runtime-status_result"
        )
        out["tool_route"] = await _command(
            "toolbox_list_tools", {}, "toolbox_list_tools_result"
        )
    return out


def main() -> int:
    token = _token()
    if not token:
        print(json.dumps({"ok": False, "error": "no IPC session token"}))
        return 2
    instance = _instance()
    try:
        result = asyncio.run(_probe(token, instance))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}))
        return 2

    tools = []
    tr = result.get("tool_route") or {}
    if isinstance(tr.get("payload"), dict):
        tools = [t.get("id") for t in (tr["payload"].get("tools") or [])]
    status_ok = bool((result.get("runtime_status") or {}).get("ok"))
    report = {
        "schema": "startup-route-probe/v1",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "port": PORT,
        "result": result,
        "tools_seen": tools,
        "routes_ok": all(
            (result.get(k) or {}).get("ok")
            for k in ("session_hello", "runtime_status", "tool_route")
        ) and bool(result.get("auth_handshake_ms")),
    }
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"startup-route-probe-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"routes_ok": report["routes_ok"],
                      "auth_ms": result.get("auth_handshake_ms"),
                      "session_ms": (result.get("session_hello") or {}).get("ms"),
                      "status_ms": (result.get("runtime_status") or {}).get("ms"),
                      "tool_route_ms": (result.get("tool_route") or {}).get("ms"),
                      "report": str(path)}, ensure_ascii=False, indent=2))
    return 0 if report["routes_ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
