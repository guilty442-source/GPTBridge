"""System Runtime Sovereign — Child Access and Status (A334)."""

from __future__ import annotations

from typing import Any

from governance.registries import children_of, primary_domain_of, validate_child_parent


# Bounded restart budget for child failure adjudication.
_MAX_CHILD_RESTARTS = 3


class SystemRuntimeChildAccessMixin:
    """Child registry and access methods."""

    _sub_sovereigns: dict[str, Any]
    app: Any
    sovereign_id: str

    def _resolve_runtime_child(self, intent: str) -> tuple[str | None, Any | None]:
        """Resolve runtime intent to child identity and instance (A334)."""
        child_id = None
        # Map intents to child identities
        intent_map = {
            "runtime.readiness": "startup-sub-sovereign",
            "runtime.health": "health-maintenance-test-sub-sovereign",
        }
        if intent in intent_map:
            child_id = intent_map[intent]

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

    def _child_status(self, child_id: str, method: str = "live_status") -> dict[str, Any]:
        child = getattr(self, "_sub_sovereigns", {}).get(child_id)
        if child is None:
            return {"role": child_id, "enabled": False, "materialized": False}
        reporter = getattr(child, method, None)
        return reporter() if callable(reporter) else {"role": child_id}

    def register_sub_sovereign(self, name: str, sovereign: Any) -> None:
        self._sub_sovereigns[name] = sovereign

    def get_sub_sovereign(self, name: str) -> Any | None:
        return self._sub_sovereigns.get(name)