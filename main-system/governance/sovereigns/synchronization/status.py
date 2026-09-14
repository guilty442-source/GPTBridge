"""Synchronization Sovereign — Status Surfaces."""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase


class SyncStatusMixin:
    """Status reporting surfaces."""

    _sub_sovereigns: dict[str, Any]
    app: Any
    _certified_update_operations: dict[str, Any]
    _child_supervision: dict[str, dict[str, Any]]
    _autonomy_task: Any
    _started: bool

    def status(self) -> dict[str, Any]:
        from governance.registries import children_of

        return self._with_status_schema({
            "sub_sovereigns": [
                self._child_status(child_id)
                for child_id in children_of("synchronization-sovereign")
            ],
            "certified_updates": self.certified_update_status(),
            "autonomy": {
                "enabled": self._autonomy_task is not None
                and not self._autonomy_task.done(),
                "supervised_children": len(self._child_supervision),
                "quarantined": [
                    child_id
                    for child_id, watch in self._child_supervision.items()
                    if watch.get("quarantined")
                ],
            },
        })

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
            "sub_sovereigns": [
                self._child_status(child_id, "orchestration_status")
                for child_id in children_of("synchronization-sovereign")
            ],
            "certified_updates": self.certified_update_status(),
        }
