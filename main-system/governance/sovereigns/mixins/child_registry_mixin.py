"""Sovereign Child Registry Mixin — sub-sovereign registration and A334 parent authority."""

from __future__ import annotations

from typing import Any

from core_system.codex_decision import (
    SovereignOutcome,
    accepted_outcome,
    refusal_outcome,
    verified_basis,
)


class ChildRegistryBase:
    """Mixin providing sub-sovereign registry and A334 parent authority."""



    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Child registry — populated by the governed executor at activation
        # (A334: each sub-sovereign is registered under exactly one parent).
        self._sub_sovereigns: dict[str, Any] = {}

    def register_sub_sovereign(self, name: str, sovereign: Any) -> None:
        self._sub_sovereigns[name] = sovereign

    def get_sub_sovereign(self, name: str) -> Any | None:
        return self._sub_sovereigns.get(name)

    def _all_children(self) -> dict[str, Any]:
        """All materialized children across every registered parent (A334).

        Child supervision (A322) must observe children owned by other
        sovereigns too: each parent in the codex hierarchy registry is
        resolved to its live instance and its child registry is merged
        into one read-only projection.
        """
        from ...registries import hierarchy_status, resolve_sovereign

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

    def authorize_child_activation(self, child_identity: str) -> SovereignOutcome:
        """A334: adjudicate whether this sovereign may dispatch a child start.

        Fail-closed: the child must be registered in this sovereign's
        registry AND the codex ``sovereign_hierarchy_registry`` must declare
        this sovereign as the child's single parent.
        """
        from ...registries import parent_of

        child = self._sub_sovereigns.get(child_identity)
        if child is None:
            return refusal_outcome("CHILD_NOT_REGISTERED", ("A334", "A130"))
        if parent_of(child_identity) != self.sovereign_id:
            return refusal_outcome("NOT_CODEX_PARENT", ("A334",))
        return accepted_outcome(
            {
                "child": child_identity,
                "parent": self.sovereign_id,
                "dispatch": "authorized",
                "execution": "delegated-to-governed-executor",
            },
            verified_basis(("A334", "A130")),
        )


__all__ = ["ChildRegistryBase"]