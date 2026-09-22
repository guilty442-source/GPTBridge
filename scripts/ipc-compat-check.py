"""IPC compatibility test executor (§10.9 pending item).

Cross-checks three surfaces against the declared contract
(``config/ipc-contract.json``):

  * **backend WS command surface** — ``MAIN_COMMANDS`` +
    ``TOOL_LIFECYCLE_HANDLERS`` + in-band built-ins
    (``state_event_hello/ack/resync``, ``heartbeat_pong``,
    ``task_recovery_decision``);
  * **renderer-sent commands** — command-namespace string literals in
    ``src-ui/renderer`` (``app:*`` / ``xingcheng-*`` / ``toolbox_*`` /
    ``sync-*`` / ``state_event*`` / ``heartbeat_*``), excluding
    receive-side ``*_result`` events and pushed events;
  * **Electron main channels** — ``ipcMain.handle/on`` registrations vs
    renderer ``window.*invoke`` usage for the ``app:`` / ``dialog:`` /
    ``embedded-browser:`` namespaces.

A command the renderer sends with no backend route is a compatibility
break (fail). Routed-but-unused commands are informational.

Writes ``runtime/logs/ipc-compat-<ts>.json``.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MS = ROOT / "main-system"
SRC_CORE = MS / "src-core"
SRC_UI = MS / "src-ui"
CONTRACT = MS / "config" / "ipc-contract.json"
LOG_DIR = MS / "runtime" / "logs"

# In-band commands handled before router dispatch (server_handler.py).
BUILTIN_COMMANDS = {
    "heartbeat_pong",
    "state_event_hello",
    "state_event_ack",
    "state_event_resync",
    "task_recovery_decision",
}
# Events pushed TO the renderer — never "sent" commands even though the
# literals appear in renderer sources.
RECEIVE_EVENTS = {
    "heartbeat_ping",
    "state_event",
    "state_event_session",
    "state_event_invalidate",
    "runtime_status_push",
    "COMMAND_RECEIVED",
}
CMD_LITERAL = re.compile(
    r"['\"]((?:app:|xingcheng-|toolbox_|sync-|state_event|heartbeat_|"
    r"task_recovery_|dialog:|embedded-browser:)[a-zA-Z0-9:_-]*)['\"]"
)
ELECTRON_HANDLE = re.compile(
    r"ipcMain\.(?:handle|on)\s*\(\s*['\"]([a-zA-Z0-9:_-]+)['\"]"
)
# Renderer only reaches Electron main through preload wrappers — those
# wrappers call ``ipcRenderer.invoke('channel', ...)`` literally.
PRELOAD_INVOKE = re.compile(
    r"ipcRenderer\.(?:invoke|send)\s*\(\s*['\"]([a-zA-Z0-9:_-]+)['\"]"
)


def _backend_commands() -> set[str]:
    sys.path.insert(0, str(SRC_CORE))
    from ipc.command_router.constants import (  # type: ignore
        MAIN_COMMANDS,
        TOOL_LIFECYCLE_HANDLERS,
    )
    return set(MAIN_COMMANDS) | set(TOOL_LIFECYCLE_HANDLERS) | BUILTIN_COMMANDS


def _scan_sources(
    base: Path, pattern: re.Pattern[str], *, per_line: bool = True
) -> dict[str, list[str]]:
    hits: dict[str, set[str]] = {}
    for path in list(base.rglob("*.ts")) + list(base.rglob("*.tsx")):
        if "node_modules" in path.parts or "dist" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not per_line:
            for m in pattern.finditer(text):
                hits.setdefault(m.group(1), set()).add(
                    str(path.relative_to(ROOT)).replace("\\", "/")
                )
            continue
        for line in text.splitlines():
            # CSS class / DOM id literals share the namespace prefixes —
            # a string on a className/data-* line is never a command.
            if "className" in line or "data-testid" in line or "data-" in line:
                continue
            for m in pattern.finditer(line):
                hits.setdefault(m.group(1), set()).add(
                    str(path.relative_to(ROOT)).replace("\\", "/")
                )
    return {k: sorted(v) for k, v in hits.items()}


def main() -> int:
    contract = json.loads(CONTRACT.read_text(encoding="utf-8"))
    backend = _backend_commands()

    renderer = SRC_UI / "renderer"
    literals = _scan_sources(renderer, CMD_LITERAL)
    # Electron main handles these channels itself — renderer literals for
    # them are preload-wrapper calls, not backend WS commands.
    electron_registered = _scan_sources(
        SRC_UI / "main", ELECTRON_HANDLE, per_line=False
    )
    ws_sent = {
        n: f
        for n, f in literals.items()
        if n not in RECEIVE_EVENTS
        and not n.endswith("_result")
        and "__" not in n  # i18n / DOM ids share the namespace prefix
        and n not in electron_registered
    }

    unhandled_ws = {
        n: f for n, f in ws_sent.items() if n not in backend
    }
    # Electron surface: every channel the preload invokes must be handled
    # by ipcMain (renderer can only reach Electron via preload wrappers).
    preload_invokes = _scan_sources(
        SRC_UI / "main", PRELOAD_INVOKE, per_line=False
    )
    unrouted_electron = {
        n: f for n, f in preload_invokes.items()
        if n not in electron_registered
    }
    unused_backend = sorted(
        c for c in backend
        if c not in ws_sent and c not in BUILTIN_COMMANDS
    )

    report = {
        "schema": "ipc-compat-check/v1",
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "contract_version": contract.get("contract_version"),
        "backend_command_count": len(backend),
        "renderer_ws_sent": {k: v for k, v in sorted(ws_sent.items())},
        "electron_channels": {
            "registered": sorted(electron_registered),
            "invoked": {k: v for k, v in sorted(preload_invokes.items())},
        },
        "compat_breaks": {
            "renderer_sent_no_backend_route": {
                k: v for k, v in sorted(unhandled_ws.items())
            },
            "electron_invoked_no_handler": {
                k: v for k, v in sorted(unrouted_electron.items())
            },
        },
        "info": {"backend_routed_unused": unused_backend},
    }
    breaks = (
        len(unhandled_ws) + len(unrouted_electron)
    )
    report["passed"] = breaks == 0

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / f"ipc-compat-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "passed": report["passed"],
        "backend_commands": len(backend),
        "renderer_sent": len(ws_sent),
        "unhandled_ws": sorted(unhandled_ws),
        "unrouted_electron": sorted(unrouted_electron),
        "unused_backend_count": len(unused_backend),
        "report": str(path),
    }, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
