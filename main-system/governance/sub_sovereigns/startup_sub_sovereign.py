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

    def record_phase(self, phase: str, status: str, details: dict[str, Any] | None = None) -> None:
        self._startup_phases[phase] = {
            "status": status,
            "details": details or {},
            "recorded_at": self._iso_now(),
        }

    def get_phase(self, phase: str) -> dict[str, Any] | None:
        return self._startup_phases.get(phase)

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["startup_phases"] = self._startup_phases
        return base


__all__ = ["StartupSubSovereign"]