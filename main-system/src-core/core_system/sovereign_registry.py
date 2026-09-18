"""Sub-Sovereign Registry.

Lazy-loading registry for sub-sovereign instances with codex parent hierarchy.
"""

from __future__ import annotations

import asyncio
from typing import Any

from governance.registries import children_of, resolve_sovereign


class SubSovereignRegistry:
    """Registry for managing sub-sovereign instances with lazy loading."""

    def __init__(self, app: Any) -> None:
        self._app = app
        self._instances: dict[str, Any] = {}
        self._class_refs: dict[str, str] = {
            "system-sub-sovereign": "SystemSubSovereign",
            "startup-sub-sovereign": "StartupSubSovereign",
            "directory-sub-sovereign": "DirectorySubSovereign",
            "identity-group-sub-sovereign": "IdentityGroupSubSovereign",
            "resource-dependency-sync-sub-sovereign": "ResourceDependencySyncSubSovereign",
            "channel-contract-sync-sub-sovereign": "ChannelContractSyncSubSovereign",
            "policy-architecture-sub-sovereign": "PolicyArchitectureSubSovereign",
            "health-maintenance-test-sub-sovereign": "HealthMaintenanceTestSubSovereign",
            "data-governance-sub-sovereign": "DataGovernanceSubSovereign",
            "priority-capability-sub-sovereign": "PriorityCapabilitySubSovereign",
            "change-acceptance-sub-sovereign": "ChangeAcceptanceSubSovereign",
            "dependency-sync-sub-sovereign": "DependencySyncSubSovereign",
            "release-update-sync-sub-sovereign": "ReleaseUpdateSyncSubSovereign",
            "runtime-state-sync-sub-sovereign": "RuntimeStateSyncSubSovereign",
            "repair-backup-sync-sub-sovereign": "RepairBackupSyncSubSovereign",
            "cleanup-retention-sync-sub-sovereign": "CleanupRetentionSyncSubSovereign",
            "learning-evidence-sync-sub-sovereign": (
                "governance.sovereigns.xingcheng.learning_sub_sovereign:"
                "LearningEvidenceSyncSubSovereign"
            ),
            "automatic-log-sync-sub-sovereign": "AutomaticLogSyncSubSovereign",
        }

    def get(self, name: str) -> Any | None:
        """Get or lazy-load a sub-sovereign by name."""
        if name in self._instances:
            return self._instances[name]

        class_ref = self._class_refs.get(name)
        if not class_ref:
            return None

        try:
            if ":" in class_ref:
                # Dotted ``module:Class`` reference
                module_name, class_name = class_ref.split(":", 1)
                module = __import__(module_name, fromlist=[class_name])
                cls = getattr(module, class_name)
            else:
                from governance.sub_sovereigns import __all__ as _all
                if class_ref not in _all:
                    return None
                module = __import__("governance.sub_sovereigns", fromlist=[class_ref])
                cls = getattr(module, class_ref)

            instance = cls(self._app)
            self._instances[name] = instance
            return instance
        except Exception:
            return None

    def collect_status(self) -> dict[str, Any]:
        """Aggregate live status from every materialized codex child."""
        collected: dict[str, Any] = {}

        # Add locally instantiated instances
        for name, sov in self._instances.items():
            live = getattr(sov, "live_status", None)
            try:
                collected[name] = live() if callable(live) else {"started": False}
            except Exception:
                collected[name] = {"error": "live_status-failed"}

        # Enumerate parents' registries via hierarchy registry
        try:
            from governance.registries import children_of, resolve_sovereign

            for parent_id in (
                "decision-sovereign",
                "permission-sovereign",
                "system-runtime-sovereign",
                "automation-sovereign",
                "星澄",
            ):
                parent = resolve_sovereign(self._app, parent_id)
                registry = getattr(parent, "_sub_sovereigns", None)
                if not registry:
                    continue
                for child_id in children_of(parent_id):
                    child = registry.get(child_id)
                    if child is not None:
                        live = getattr(child, "live_status", None)
                        try:
                            collected.setdefault(child_id, live() if callable(live) else {"started": False})
                        except Exception:
                            collected.setdefault(child_id, {"error": "live_status-failed"})
        except Exception:
            pass

        return collected

    def get(self, name: str) -> Any | None:
        """Get or lazy-load a sub-sovereign by name (alias for get)."""
        return self.get(name)


__all__ = ["SubSovereignRegistry"]