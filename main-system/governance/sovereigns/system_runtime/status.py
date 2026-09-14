"""System Runtime Sovereign — Status Surfaces."""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase


class SystemRuntimeStatusMixin:
    """Status reporting surfaces."""

    _sub_sovereigns: dict[str, Any]
    app: Any
    _runtime_state: str
    _auto_metrics: dict[str, Any]
    _child_supervision: dict[str, dict[str, Any]]
    _auto_loop_task: Any
    _started: bool

    def status(self) -> dict[str, Any]:
        from governance.registries import children_of

        return {
            "sovereign": "system-runtime-sovereign",
            "runtime_state": self._runtime_state,
            "sub_sovereigns": [
                self._child_status(child_id)
                for child_id in children_of("system-runtime-sovereign")
            ],
            "autonomy": {
                "enabled": self._auto_loop_task is not None
                and not self._auto_loop_task.done(),
                "metrics": dict(self._auto_metrics),
                "supervised_children": len(self._child_supervision),
                "quarantined": [
                    child_id
                    for child_id, watch in self._child_supervision.items()
                    if watch.get("quarantined")
                ],
            },
        }

    def live_status(self) -> dict[str, Any]:
        base = self.status()
        base["sub_sovereign_registry"] = {
            name: sov.live_status() if hasattr(sov, "live_status") else {"role": name}
            for name, sov in self._all_children().items()
        }
        return base

    def orchestration_status(self) -> dict[str, Any]:
        from governance.registries import children_of

        return {
            "state": "decision-only",
            "owner": self.sovereign_id,
            "runtime_state": self._runtime_state,
            "sub_sovereigns": [
                self._child_status(child_id, "orchestration_status")
                for child_id in children_of("system-runtime-sovereign")
            ],
            "autonomy": {
                "enabled": self._auto_loop_task is not None
                and not self._auto_loop_task.done(),
                "metrics": dict(self._auto_metrics),
            },
        }
