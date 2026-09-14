"""Synchronization Sovereign — Intent Mapping and Child Access (A334).

Codex child identity resolution and registry-backed access.
"""

from __future__ import annotations

from typing import Any

from governance.registries import children_of, module_assignment, parent_of, primary_domain_of, validate_child_parent


# Sync intent -> codex child identity (A334)
_SYNC_INTENT_CHILDREN: dict[str, str] = {
    "sync.resource-dependency": "resource-dependency-sync-sub-sovereign",
    "sync.channel-contract": "channel-contract-sync-sub-sovereign",
    "sync.release-update": "release-update-sync-sub-sovereign",
    "sync.learning-evidence": "learning-evidence-sync-sub-sovereign",
    "sync.runtime-state": "runtime-state-sync-sub-sovereign",
    "sync.repair-backup": "repair-backup-sync-sub-sovereign",
    "sync.cleanup-retention": "cleanup-retention-sync-sub-sovereign",
    "sync.automatic-log": "automatic-log-sync-sub-sovereign",
    "sync.dependency": "dependency-sync-sub-sovereign",
}

# A330: update types covered by the certified-update execution exception
_A330_UPDATE_TYPES: frozenset[str] = frozenset(
    {"backend-release", "codex", "governance-policy", "directory"}
)

# Bounded restart budget for child failure adjudication (A322 retry/cancel)
_MAX_CHILD_RESTARTS = 3


class SyncChildAccessMixin:
    """Child registry and access methods for synchronization sovereign."""

    _sub_sovereigns: dict[str, Any]
    app: Any
    sovereign_id: str

    def _resolve_sync_child(self, intent: str) -> tuple[str | None, Any | None]:
        """Resolve sync intent to child identity and instance (A334 registry-backed)."""
        child_id = _SYNC_INTENT_CHILDREN.get(intent)
        if child_id is None:
            return None, None
        if not validate_child_parent(child_id, self.sovereign_id):
            return None, None
        child = getattr(self, "_sub_sovereigns", {}).get(child_id)
        return child_id, child

    def _all_children(self) -> dict[str, Any]:
        """All materialized children across every parent's registry."""
        from governance.registries import hierarchy_status, resolve_sovereign

        merged: dict[str, Any] = {}
        for parent_id in hierarchy_status()["parents"]:
            parent = (
                self
                if parent_id == self.sovereign_id
                else resolve_sovereign(self.app, parent_id)
            )
            if parent is not None:
                merged.update(getattr(parent, "_sub_sovereigns", {}))
        return merged

    def _child_status(self, child_id: str) -> dict[str, Any]:
        child = getattr(self, "_sub_sovereigns", {}).get(child_id)
        if child is None:
            return {"role": child_id, "enabled": False, "materialized": False}
        reporter = getattr(child, "live_status", None)
        return reporter() if callable(reporter) else {"role": child_id}

    def register_sub_sovereign(self, name: str, sovereign: Any) -> None:
        self._sub_sovereigns[name] = sovereign

    def get_sub_sovereign(self, name: str) -> Any | None:
        return self._sub_sovereigns.get(name)