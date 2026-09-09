"""State change notifier — A67 immediate event propagation.

Per Governance Codex A67, status events must be propagated *immediately* to
all active UI when a state transition occurs — not only on the periodic
2-second push interval.  This module tracks the last known runtime state
and triggers an immediate push to all connected UI shells whenever the
readiness state changes.

The notifier is event-driven: it compares the new readiness snapshot
against the last pushed one and fires an immediate ``runtime_status_push``
event to every active UIShell when any of the four readiness conditions or
the overall runtime state changes.  The existing 2-second push loop
remains as a safety-net keepalive; this notifier eliminates the up-to-2s
latency for state transitions.

A67 SCOPE: ``main-ui+all-independent-tool-ui``.  In addition to the
in-process WebSocket push, the notifier writes the readiness snapshot to
an information-layer state file (``runtime-readiness.json``) so that
independent tool UIs that have not yet registered a UIShell (or that
connect via the governed source-runtime IPC rather than the main UI
socket) can observe the latest readiness state without waiting for a
push they may never receive.  This closes the gap for unregistered
independent tool UIs.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Final

from tasks.readiness_gate import ReadinessGate, ReadinessSnapshot

STATE_NOTIFIER_VERSION: Final[str] = "1.00000"

# Information-layer state file for cross-UI readiness propagation.
READINESS_STATE_RELATIVE: Final[tuple[str, ...]] = (
    "main-system", "runtime", "state", "runtime-readiness.json",
)


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


class StateChangeNotifier:
    """Tracks readiness state and pushes immediately on transitions.

    Lives alongside the periodic push loop in the IPC server.  Call
    ``maybe_notify`` after any event that may change readiness (command
    router initialization, governance audit, dependency start, WebSocket
    connect/disconnect).  It compares against the last snapshot and, if
    the state changed, pushes immediately to all active UI shells AND
    writes the snapshot to the information-layer state file so unregistered
    independent tool UIs can observe it.
    """

    VERSION = STATE_NOTIFIER_VERSION

    def __init__(self, app: Any) -> None:
        self.app = app
        self._gate = ReadinessGate(app)
        self._last_snapshot: ReadinessSnapshot | None = None
        self._state_file = self._resolve_state_file()

    def _resolve_state_file(self) -> Path:
        project_root = getattr(self.app, "project_root", None)
        if project_root is None:
            project_root = Path.cwd()
        return Path(project_root).joinpath(*READINESS_STATE_RELATIVE)

    def _active_shells(self) -> set:
        shells = getattr(self.app, "_active_ui_shells", None)
        if shells is None:
            shells = set()
            self.app._active_ui_shells = shells
        return shells

    def _build_payload(self, snapshot: ReadinessSnapshot) -> dict[str, Any]:
        """Build the runtime_status_push payload from a readiness snapshot."""
        status_service = getattr(self.app, "runtime_status_service", None)
        payload: dict[str, Any] = {}
        if status_service is not None:
            try:
                payload = status_service.startup_status()
            except Exception:
                payload = {}
        payload.update(
            {
                "push": True,
                "immediate": True,
                "backend_runtime_ready": snapshot.backend_runtime_ready,
                "governance_ready": snapshot.governance_ready,
                "dependencies_ready": snapshot.dependencies_ready,
                "authenticated_ipc_connected": snapshot.authenticated_ipc_connected,
                "dependencies": [d.as_dict() for d in snapshot.dependencies],
                "overall_ready": snapshot.overall_ready,
                "runtime_state": snapshot.runtime_state,
                "systemReady": snapshot.overall_ready,
                "evaluated_at": snapshot.evaluated_at,
            }
        )
        return payload

    def _state_changed(self, new: ReadinessSnapshot) -> bool:
        """Return True if the new snapshot differs from the last pushed one."""
        last = self._last_snapshot
        if last is None:
            return True
        return (
            last.backend_runtime_ready != new.backend_runtime_ready
            or last.governance_ready != new.governance_ready
            or last.dependencies_ready != new.dependencies_ready
            or last.authenticated_ipc_connected != new.authenticated_ipc_connected
            or last.overall_ready != new.overall_ready
            or last.runtime_state != new.runtime_state
        )

    def _write_readiness_state(self, snapshot: ReadinessSnapshot) -> None:
        """Write the readiness snapshot to the information-layer state file.

        This is the A67 propagation channel for independent tool UIs that
        are not registered as UIShell connections on the main backend
        socket (e.g. companion windows whose WebSocket session is to a
        governed source-runtime, or tools that poll /health before
        establishing a push subscription).  Writing the snapshot here lets
        them observe the latest readiness state atomically.
        """
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": STATE_NOTIFIER_VERSION,
                "snapshot": snapshot.as_dict(),
                "updated_at": _iso_now(),
            }
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self._state_file)
        except OSError:
            pass  # Best-effort; never block the push path.

    async def maybe_notify(self) -> ReadinessSnapshot | None:
        """Evaluate readiness and push immediately if the state changed.

        Returns the new snapshot if a push was performed, or None if the
        state was unchanged (no push needed).  Best-effort: never raises.
        """
        try:
            snapshot = self._gate.evaluate()
            if not self._state_changed(snapshot):
                return None
            self._last_snapshot = snapshot
            # A67: propagate to the information layer first so unregistered
            # independent tool UIs see the transition even if no UIShell
            # push reaches them.
            self._write_readiness_state(snapshot)
            shells = self._active_shells()
            if not shells:
                return snapshot
            payload = self._build_payload(snapshot)
            dead: list[Any] = []
            for shell in list(shells):
                send = getattr(shell, "send_event", None)
                if not callable(send):
                    continue
                try:
                    await send("runtime_status_push", payload)
                except Exception:
                    dead.append(shell)
            for shell in dead:
                shells.discard(shell)
            return snapshot
        except Exception:
            return None

    def current_snapshot(self) -> ReadinessSnapshot | None:
        return self._last_snapshot


__all__ = ["STATE_NOTIFIER_VERSION", "StateChangeNotifier"]
