"""Decision Sovereign — Status Surfaces (A128/A334).

Provides status(), live_status(), and orchestration_status() for
governance visibility. Read-only surfaces; no mutations.
"""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase
from core_system.sovereign_utils import _iso_now


class DecisionStatusMixin:
    """Status reporting surfaces."""

    _sub_sovereigns: dict[str, Any]
    app: Any
    platform_id: str
    module_id: str
    governance_rule_coordination: Any
    _certified_updates: dict[str, Any]
    _child_supervision: dict[str, dict[str, Any]]
    _autonomy_task: Any
    _started: bool
    workspace_root: Any
    runtime_state_path: Any

    def status(self) -> dict[str, Any]:
        from governance.registries import children_of

        state = self._load_state()
        maintenance_sovereign = getattr(self.app, "maintenance_sovereign", None)
        permission_sovereign = getattr(self.app, "permission_sovereign", None)
        return self._with_status_schema({
            "platform_id": self.platform_id,
            "module_id": self.module_id,
            "owned_by": self.module_id,
            "dependency_state": state.get("dependency_state", ""),
            "started_at": state.get("started_at", ""),
            "executor": "governed-executor-only",
            "sub_sovereigns": [
                self._child_status(child_id)
                for child_id in children_of("decision-sovereign")
            ],
            "coordinated_sub_sovereigns": [
                self._child_status("runtime-state-sync-sub-sovereign"),
                self._child_status("resource-dependency-sync-sub-sovereign"),
                self._child_status("data-governance-sub-sovereign"),
                self._child_status("channel-contract-sync-sub-sovereign"),
                self._child_status("dependency-sync-sub-sovereign"),
            ],
            "peer_systems": {
                "learning": self._child_status(
                    "learning-evidence-sync-sub-sovereign", "status"
                ),
                "programming": self._child_status(
                    "release-update-sync-sub-sovereign", "status"
                ),
            },
            "health_owner": "health-maintenance-test-sub-sovereign",
            "governance_rules": self.governance_rule_coordination.coordination_status(),
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
            "runtime-state-sync": self._child_status("runtime-state-sync-sub-sovereign"),
            "resource-dependency-sync": self._child_status("resource-dependency-sync-sub-sovereign"),
            "data-governance": self._child_status("data-governance-sub-sovereign"),
            "channel-contract-sync": self._child_status("channel-contract-sync-sub-sovereign"),
            "dependency-sync": self._child_status("dependency-sync-sub-sovereign"),
            "maintenance": (
                maintenance_sovereign.live_status()
                if maintenance_sovereign is not None
                else {"enabled": False}
            ),
            "permission": (
                permission_sovereign.coordination_status()
                if permission_sovereign is not None
                else {"enabled": False}
            ),
        })

    def live_status(self) -> dict[str, Any]:
        base = self.status()
        base["sub_sovereign_registry"] = {
            name: sov.live_status() if hasattr(sov, "live_status") else {"role": name}
            for name, sov in self._all_children().items()
        }
        return base

    def orchestration_status(self) -> dict[str, Any]:
        """Unified subsystem health for the governing orchestrator."""
        from governance.registries import children_of

        maintenance_sovereign = getattr(self.app, "maintenance_sovereign", None)
        permission_sovereign = getattr(self.app, "permission_sovereign", None)
        return {
            "state": "delegated",
            "owner": self.module_id,
            "sub_sovereigns": [
                self._child_status(child_id, "orchestration_status")
                for child_id in children_of("decision-sovereign")
            ],
            "coordinated_sub_sovereigns": [
                self._child_status(
                    "runtime-state-sync-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "resource-dependency-sync-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "data-governance-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "channel-contract-sync-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "dependency-sync-sub-sovereign", "orchestration_status"
                ),
            ],
            "peer_systems": {
                "learning": self._child_status(
                    "learning-evidence-sync-sub-sovereign", "status"
                ),
                "programming": self._child_status(
                    "release-update-sync-sub-sovereign", "status"
                ),
            },
            "health_owner": "health-maintenance-test-sub-sovereign",
            "governance_rules": self.governance_rule_coordination.orchestration_status(),
            "runtime-state-sync": self._child_status(
                "runtime-state-sync-sub-sovereign", "orchestration_status"
            ),
            "maintenance": (
                maintenance_sovereign.orchestration_status()
                if maintenance_sovereign is not None
                else {"enabled": False}
            ),
            "permission": (
                permission_sovereign.orchestration_status()
                if permission_sovereign is not None
                else {"enabled": False}
            ),
            "resource-dependency-sync": self._child_status(
                "resource-dependency-sync-sub-sovereign", "orchestration_status"
            ),
            "data-governance": self._child_status(
                "data-governance-sub-sovereign", "orchestration_status"
            ),
            "channel-contract-sync": self._child_status(
                "channel-contract-sync-sub-sovereign", "orchestration_status"
            ),
            "dependency-sync": self._child_status(
                "dependency-sync-sub-sovereign", "orchestration_status"
            ),
            "subsystems": [
                self.governance_rule_coordination.orchestration_status(),
                self._child_status(
                    "runtime-state-sync-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "resource-dependency-sync-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "data-governance-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "channel-contract-sync-sub-sovereign", "orchestration_status"
                ),
                self._child_status(
                    "dependency-sync-sub-sovereign", "orchestration_status"
                ),
            ],
        }