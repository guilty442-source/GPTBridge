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
"""

from __future__ import annotations

import asyncio
from typing import Any, Final

from tasks.readiness_gate import ReadinessGate, ReadinessSnapshot

STATE_NOTIFIER_VERSION: Final[str] = "1.0.0"


class StateChangeNotifier:
    """Tracks readiness state and pushes immediately on transitions.

    Lives alongside the periodic push loop in the IPC server.  Call
    ``maybe_notify`` after any event that may change readiness (command
    router initialization, governance audit, dependency start, WebSocket
    connect/disconnect).  It compares against the last snapshot and, if
    the state changed, pushes immediately to all active UI shells.
    """

    VERSION = STATE_NOTIFIER_VERSION

    def __init__(self, app: Any) -> None:
        self.app = app
        self._gate = ReadinessGate(app)
        self._last_snapshot: ReadinessSnapshot | None = None

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
