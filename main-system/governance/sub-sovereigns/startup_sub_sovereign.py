"""Startup Sub-Sovereign — 啟動子主權（子屬運行主宰，無決策、無執行）。

法典依據:
- sovereign_id: startup-sub-sovereign (position 19)
- area: startup
- rank: child-of-runtime-sovereign-no-decision-no-execution
- basis: A303|A304|parent:runtime-sovereign
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase


class StartupSubSovereign(SubSovereignBase):
    """啟動子主權：啟動階段協調。"""

    sovereign_id = "startup-sub-sovereign"
    parent_sovereign_id = "system-runtime-sovereign"

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._startup_phases: dict[str, dict[str, Any]] = {}

    def record_phase(self, phase: str, status: str, details: dict[str, Any] | None = None) -> bool:
        """Record a boot phase — refuse malformed phase records
        (fail-closed coordination)."""
        if not phase or not status:
            return False
        self._startup_phases[phase] = {
            "status": status,
            "details": details or {},
            "recorded_at": self._iso_now(),
        }
        return True

    def get_phase(self, phase: str) -> dict[str, Any] | None:
        return self._startup_phases.get(phase)

    def readiness_handoff(self) -> dict[str, Any]:
        """A303/A304: verify boot phases and hand readiness to the
        runtime-sovereign parent.

        Startup coordinates boot only — it does not decide or execute.
        A handoff is valid only when every recorded phase reached a
        ``ready``/``done`` state; the verified outcome is reported to the
        codex parent via ``report_to_parent``.
        """
        phases = self._startup_phases
        incomplete = {
            name: state.get("status")
            for name, state in phases.items()
            if state.get("status") not in ("ready", "done", "converged")
        }
        ready = bool(phases) and not incomplete
        report = {
            "ready": ready,
            "phases": {name: s.get("status") for name, s in phases.items()},
            "incomplete": incomplete,
            "handed_to": self.parent_sovereign_id if ready else None,
        }
        self.report_to_parent("converged" if ready else "incomplete")
        return report

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["startup_phases"] = self._startup_phases
        return base


__all__ = ["StartupSubSovereign"]