"""Decision Sovereign — Child Registry and Access (A334/A323).

Codex child access, parent resolution, and child status reporting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from governance.registries import children_of, parent_of, hierarchy_status, resolve_sovereign
from core_system.sovereign_utils import _iso_now


# Legacy attribute names -> codex child identity (A334)
_CHILD_ATTRIBUTE_MAP: dict[str, str] = {
    "runtime_sovereign": "runtime-state-sync-sub-sovereign",
    "resource_sovereign": "resource-dependency-sync-sub-sovereign",
    "data_sovereign": "data-governance-sub-sovereign",
    "integration_sovereign": "channel-contract-sync-sub-sovereign",
    "third_party_sovereign": "dependency-sync-sub-sovereign",
    "learning_system_sovereign": "learning-evidence-sync-sub-sovereign",
    "system_programming_sovereign": "release-update-sync-sub-sovereign",
}


class DecisionChildAccessMixin:
    """Child registry and access methods."""

    _sub_sovereigns: dict[str, Any]
    app: Any
    workspace_root: Path
    sovereign_id: str

    def __getattr__(self, name: str) -> Any:
        child_id = _CHILD_ATTRIBUTE_MAP.get(name)
        if child_id is not None:
            return self._child(child_id)
        raise AttributeError(
            f"{type(self).__name__!r} object has no attribute {name!r}"
        )

    @property
    def permission_sovereign(self) -> Any:
        """Read-only passthrough to the app's permission sovereign."""
        return getattr(self.app, "permission_sovereign", None)

    def _parent_for(self, child_id: str) -> Any | None:
        """A334: resolve a child identity to its codex-registered parent."""
        parent_id = parent_of(child_id)
        if parent_id == self.sovereign_id:
            return self
        return resolve_sovereign(self.app, parent_id)

    def _child(self, child_id: str) -> Any:
        """Resolve a child through its codex-registered parent's registry."""
        parent = self._parent_for(child_id)
        if parent is None:
            return None
        return getattr(parent, "_sub_sovereigns", {}).get(child_id)

    def _all_children(self) -> dict[str, Any]:
        """All materialized children across every parent's registry."""
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

    def _child_status(self, child_id: str, method: str = "live_status") -> dict[str, Any]:
        child = self._child(child_id)
        if child is None:
            return {"role": child_id, "enabled": False, "materialized": False}
        reporter = getattr(child, method, None)
        return reporter() if callable(reporter) else {"role": child_id}

    def register_sub_sovereign(self, name: str, sovereign: Any) -> None:
        self._sub_sovereigns[name] = sovereign

    def get_sub_sovereign(self, name: str) -> Any | None:
        return self._sub_sovereigns.get(name)