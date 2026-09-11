"""embedded_browser_client — Python IPC client for the Electron embedded browser.

Replaces Playwright/Chrome/Edge external browser automation with IPC calls
to the Electron main process's BrowserView-based embedded browser.

All tool modules use this client instead of launching
external browser processes.  No Playwright, no Chrome, no Edge dependency.

Governance: A44/E30 (four-functions-local) + A49/E35 (formal-tools-local).
"""

from __future__ import annotations

import json
import socket
import uuid
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
    ) -> dict[str, Any]:
        """Create or reuse an embedded browser session."""
        session_id = f"{owner_module}-{uuid.uuid4().hex[:8]}"
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

    def _call_ipc(self, channel: str, args: dict[str, Any]) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
        """Actual bridge implementation — overridden in production."""
        # In production, this connects to the main system's IPC bridge.
        # For now, return a structured response indicating the module
        # is loaded but needs the Electron bridge to be connected.
        # The main system's python-backend.ts handles the actual forwarding.
        raise ConnectionError("EMBEDDED_BROWSER_BRIDGE_NOT_CONNECTED")


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
    ) -> dict[str, Any]:
        session_id = f"{owner_module}-{uuid.uuid4().hex[:8]}"
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
