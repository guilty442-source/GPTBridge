"""InvestmentLifecycleController — tool state machine (§18).

STOPPED → STARTING → READY → RUNNING ⇄ PAUSED
                 ↘ DEGRADED ⇄ RECOVERING ↘ STOPPING → STOPPED / FAILED

The tool is still managed by GPTBridge's governed tool lifecycle — this
controller only reflects the investment domain's internal readiness;
it never creates a second launcher.
"""
from __future__ import annotations

import time
from typing import Any

STATES = ("STOPPED", "STARTING", "READY", "RUNNING", "PAUSED",
          "DEGRADED", "RECOVERING", "STOPPING", "FAILED")

_ALLOWED = {
    "STOPPED": {"STARTING"},
    "STARTING": {"READY", "FAILED"},
    "READY": {"RUNNING", "STOPPING", "DEGRADED"},
    "RUNNING": {"PAUSED", "DEGRADED", "STOPPING", "RECOVERING"},
    "PAUSED": {"RUNNING", "STOPPING", "RECOVERING"},
    "DEGRADED": {"RUNNING", "RECOVERING", "STOPPING", "FAILED"},
    "RECOVERING": {"RUNNING", "DEGRADED", "PAUSED", "FAILED", "STOPPING"},
    "STOPPING": {"STOPPED", "FAILED"},
    "FAILED": {"STARTING"},
}


class InvestmentLifecycleController:
    def __init__(self) -> None:
        self.state = "STOPPED"
        self._history: list[dict[str, Any]] = []
        self._changed_at = time.time()

    def transition(self, to: str, *, reason: str = "",
                   actor: str = "system") -> dict[str, Any]:
        to = str(to).upper()
        if to not in STATES:
            return {"ok": False, "error_code": "STATE_UNKNOWN"}
        if to not in _ALLOWED.get(self.state, set()):
            return {"ok": False, "error_code": "TRANSITION_DENIED",
                    "from": self.state, "to": to}
        prev = self.state
        self.state = to
        self._changed_at = time.time()
        self._history.append({
            "from": prev, "to": to, "reason": reason,
            "actor": actor, "at": self._changed_at})
        self._history = self._history[-200:]
        return {"ok": True, "state": to}

    def begin_shutdown(self) -> dict[str, Any]:
        """STOPPING — no new simulated trades; callers checkpoint state
        and release resources. Never loses unpersisted state."""
        return self.transition("STOPPING", reason="shutdown")

    def status(self) -> dict[str, Any]:
        return {"ok": True, "state": self.state,
                "state_since": self._changed_at,
                "history": list(self._history[-10:])}
