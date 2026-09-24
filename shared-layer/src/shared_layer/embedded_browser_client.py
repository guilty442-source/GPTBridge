"""embedded_browser_client — Python IPC client for the Electron embedded browser.

Replaces Playwright/Chrome/Edge external browser automation with IPC calls
to the Electron main process's BrowserView-based embedded browser.

All tool modules use this client instead of launching
external browser processes.  No Playwright, no Chrome, no Edge dependency.

Governance: A44/E30 (four-functions-local) + A49/E35 (formal-tools-local).
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any


class EmbeddedBrowserClient:
    """IPC client for the Electron embedded browser.

    Communicates with the Electron main process via a local Unix domain
    socket or named pipe.  In practice, the main system's Python backend
    bridges IPC calls to the Electron renderer/main process.

    This client provides the same interface as the old Playwright-based
    BrowserAutomationSession, but all browser operations happen inside
    the Electron app — no external browser process is launched.
    """

    def __init__(self, bridge_socket_path: str | None = None) -> None:
        self._bridge_path = bridge_socket_path
        self._connected = False

    def create_session(
        self,
        owner_module: str,
        url: str,
        bounds: dict[str, int] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        """Create or reuse an embedded browser session.

        ``session_id`` lets a caller reuse the same BrowserView across
        calls (the Electron side re-navigates the existing view when the
        id already exists) instead of accumulating one view per send.
        """
        session_id = session_id or f"{owner_module}-{uuid.uuid4().hex[:8]}"
        return self._call_ipc("embedded-browser:create", {
            "id": session_id,
            "ownerModule": owner_module,
            "url": url,
            "bounds": bounds,
        })

    def navigate(self, session_id: str, url: str) -> dict[str, Any]:
        """Navigate a session to a new URL."""
        return self._call_ipc("embedded-browser:navigate", {
            "id": session_id,
            "url": url,
        })

    def execute_script(self, session_id: str, script: str, args: tuple = ()) -> dict[str, Any]:
        """Execute JavaScript in a session and return the result."""
        return self._call_ipc("embedded-browser:execute", {
            "id": session_id,
            "script": script,
            "args": list(args),
        })

    def show(self, session_id: str) -> dict[str, Any]:
        """Show a session (bring to front)."""
        return self._call_ipc("embedded-browser:show", {"id": session_id})

    def hide(self, session_id: str) -> dict[str, Any]:
        """Hide a session."""
        return self._call_ipc("embedded-browser:hide", {"id": session_id})

    def close(self, session_id: str) -> dict[str, Any]:
        """Close and destroy a session."""
        return self._call_ipc("embedded-browser:close", {"id": session_id})

    def resize(
        self, session_id: str, bounds: dict[str, int]
    ) -> dict[str, Any]:
        """Resize a session."""
        return self._call_ipc("embedded-browser:resize", {
            "id": session_id,
            "bounds": bounds,
        })

    def get_url(self, session_id: str) -> str | None:
        """Get the current URL of a session."""
        result = self._call_ipc("embedded-browser:url", {"id": session_id})
        if result.get("ok"):
            return str(result.get("url") or "")
        return None

    def list_sessions(self) -> list[dict[str, Any]]:
        """List all active sessions."""
        result = self._call_ipc("embedded-browser:list", {})
        return result if isinstance(result, list) else []

    def close_module_sessions(self, owner_module: str) -> int:
        """Close all sessions for a module."""
        result = self._call_ipc("embedded-browser:close-module", {
            "ownerModule": owner_module,
        })
        return int(result.get("closed") or 0)

    def _call_ipc(
        self, channel: str, args: dict[str, Any]
    ) -> dict[str, Any] | list[Any]:
        """Call an IPC handler in the Electron main process.

        In the current architecture, this goes through the main system's
        Python backend bridge, which forwards to Electron's ipcMain.
        If the bridge is not available, returns a degraded-mode result.
        """
        # The actual IPC bridge is handled by the main system's
        # python-backend IPC channel.  This is a stub that returns
        # a structured response indicating the browser module is loaded
        # but the Electron bridge may not be connected.
        try:
            return self._bridge_call(channel, args)
        except (ConnectionError, OSError, FileNotFoundError):
            return {
                "ok": False,
                "message": "EMBEDDED_BROWSER_BRIDGE_UNAVAILABLE",
                "channel": channel,
            }

    def _bridge_call(
        self, channel: str, args: dict[str, Any]
    ) -> dict[str, Any] | list[Any]:
        """Call the Electron-main embedded-browser bridge over loopback.

        The main Electron process publishes ``{host, port, token}`` to the
        runtime state file while it runs; tool backends POST to it.  The
        endpoint stays on 127.0.0.1 and requires the per-launch token, so no
        unauthenticated caller can drive a browser session.
        """
        import urllib.error
        import urllib.request

        state_path = self._bridge_state_path()
        if state_path is None:
            raise ConnectionError("EMBEDDED_BROWSER_BRIDGE_NOT_PUBLISHED")
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise ConnectionError("EMBEDDED_BROWSER_BRIDGE_STATE_INVALID") from error
        host = str(state.get("host") or "127.0.0.1")
        port = int(state.get("port") or 0)
        token = str(state.get("token") or "")
        if port <= 0 or not token:
            raise ConnectionError("EMBEDDED_BROWSER_BRIDGE_STATE_INVALID")
        request = urllib.request.Request(
            f"http://{host}:{port}/invoke",
            data=json.dumps({"channel": channel, "args": args}).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "X-GPTBridge-Bridge-Token": token,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise ConnectionError("EMBEDDED_BROWSER_BRIDGE_UNAVAILABLE") from error
        if isinstance(payload, list):
            return payload
        if not isinstance(payload, dict):
            return {"ok": False, "message": "BRIDGE_RESPONSE_INVALID"}
        return payload

    def bridge_identity(self) -> dict[str, Any]:
        """Report which embedded-browser bridge this client will reach.

        ``kind`` is ``tool-window`` when the caller's own governed tool
        window publishes a bridge (automation is visible and shares the
        window's session), ``main-system`` for the main-system bridge
        (sessions cannot be shown), or ``unavailable``.
        """
        state_path = self._bridge_state_path()
        if state_path is None:
            return {"kind": "unavailable", "path": "", "pid": 0}
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"kind": "unavailable", "path": str(state_path), "pid": 0}
        return {
            "kind": str(state.get("kind") or "main-system"),
            "path": str(state_path),
            "pid": int(state.get("pid") or 0),
            "tool_id": str(state.get("tool_id") or ""),
        }

    @staticmethod
    def _bridge_state_path() -> Path | None:
        """Locate the published bridge state file inside the workspace.

        A tool backend prefers the bridge published by its own tool window
        (``tool-window-browser-bridge.json``): only that window can display
        sessions and share the tool's authenticated browser state.  The
        main-system bridge remains as a fallback for legacy callers.
        """

        tool_dir = os.environ.get("GPTBRIDGE_TOOL_DIR") or os.environ.get(
            "GPTBRIDGE_PROJECT_ROOT"
        )
        if tool_dir:
            tool_bridge = (
                Path(tool_dir)
                / "runtime"
                / "ipc"
                / "tool-window-browser-bridge.json"
            )
            if tool_bridge.is_file():
                return tool_bridge
        candidates: list[Path] = []
        environment_root = os.environ.get(
            "GPTBRIDGE_GOVERNANCE_PROJECT_ROOT"
        ) or os.environ.get("GPTBRIDGE_PROJECT_ROOT")
        if environment_root:
            candidates.append(Path(environment_root))
        # shared-layer/src/shared_layer/embedded_browser_client.py
        candidates.append(Path(__file__).resolve().parents[3])
        for root in candidates:
            state_path = (
                root
                / "main-system"
                / "runtime"
                / "state"
                / "embedded-browser-bridge.json"
            )
            if state_path.is_file():
                return state_path
        return None


# ---------------------------------------------------------------------------
# Convenience: in-process fallback for headless / test environments.
# ---------------------------------------------------------------------------

class InProcessEmbeddedBrowser:
    """In-process fallback that simulates browser operations without Electron.

    Used when running outside Electron (e.g., tests, headless server mode).
    Does NOT launch any external browser — just tracks session state.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, dict[str, Any]] = {}

    def create_session(
        self,
        owner_module: str,
        url: str,
        bounds: dict[str, int] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        session_id = session_id or f"{owner_module}-{uuid.uuid4().hex[:8]}"
        self._sessions[session_id] = {
            "id": session_id,
            "ownerModule": owner_module,
            "url": url,
            "createdAt": 0,
            "content": "",
            "title": "",
        }
        return {"ok": True, "id": session_id, "url": url}

    def navigate(self, session_id: str, url: str) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if not session:
            return {"ok": False, "message": "SESSION_NOT_FOUND"}
        session["url"] = url
        return {"ok": True}

    def execute_script(self, session_id: str, script: str, args: tuple = ()) -> dict[str, Any]:
        session = self._sessions.get(session_id)
        if not session:
            return {"ok": False, "message": "SESSION_NOT_FOUND"}
        # In-process fallback: return empty result
        return {"ok": True, "result": None}

    def show(self, session_id: str) -> dict[str, Any]:
        if session_id not in self._sessions:
            return {"ok": False, "message": "SESSION_NOT_FOUND"}
        return {"ok": True}

    def hide(self, session_id: str) -> dict[str, Any]:
        if session_id not in self._sessions:
            return {"ok": False, "message": "SESSION_NOT_FOUND"}
        return {"ok": True}

    def close(self, session_id: str) -> dict[str, Any]:
        if session_id not in self._sessions:
            return {"ok": False, "message": "SESSION_NOT_FOUND"}
        del self._sessions[session_id]
        return {"ok": True}

    def resize(
        self, session_id: str, bounds: dict[str, int]
    ) -> dict[str, Any]:
        if session_id not in self._sessions:
            return {"ok": False, "message": "SESSION_NOT_FOUND"}
        return {"ok": True}

    def get_url(self, session_id: str) -> str | None:
        session = self._sessions.get(session_id)
        return session["url"] if session else None

    def list_sessions(self) -> list[dict[str, Any]]:
        return list(self._sessions.values())

    def close_module_sessions(self, owner_module: str) -> int:
        to_close = [
            sid for sid, s in self._sessions.items()
            if s["ownerModule"] == owner_module
        ]
        for sid in to_close:
            del self._sessions[sid]
        return len(to_close)


__all__ = ["EmbeddedBrowserClient", "InProcessEmbeddedBrowser"]
