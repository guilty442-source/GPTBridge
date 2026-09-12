"""Synchronization Sovereign — 同步主宰（專門決策主宰，A330 認證更新執行例外）。

法典依據:
- sovereign_id: synchronization-sovereign (position 18)
- area: synchronization-decision
- rank: specialized-decision-sovereign-with-A330-certified-update-execution-exception
- basis: A301
- duties: resource-sync|channel-sync|release-sync|learning-sync|runtime-sync|repair-sync|cleanup-sync|log-sync
- powers: adjudicate-sync-decisions|A330-certified-update-execution
- prohibitions: FORBID:general-execution (except A330)

A334: this sovereign is the codex-registered single parent of every
synchronization sub-sovereign.  Child identity -> primary domain and the
delegation target are resolved from ``sovereign_hierarchy_registry`` at
adjudication time; nothing here hard-codes the hierarchy.

Lifecycle boundary: the governed executor materializes and starts each
child ONLY after ``authorize_child_activation`` (or the dispatch wrapper
``dispatch_child_activation``) returns an accepted outcome.  The sovereign
adjudicates; the executor executes.
"""

from __future__ import annotations

from typing import Any

from ._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome

from ..registries import (
    parent_of,
    primary_domain_of,
    validate_child_parent,
)

# Sync intent -> codex child identity.  The parent assertion is re-validated
# against the registry on every adjudication (fail-closed, A334).
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


class SynchronizationSovereign(SovereignBase):
    """同步主宰：專門決策，協調各類同步子主宰，A330例外執行。"""

    sovereign_id = "synchronization-sovereign"

    async def _adjudicate(self, request: SovereignRequest) -> SovereignOutcome:
        """裁決：同步決策、A330認證更新、子主宰協調。"""
        intent = request.intent

        child_id = _SYNC_INTENT_CHILDREN.get(intent)
        if child_id is not None:
            return self._adjudicate_sync_dispatch(request, child_id)

        if intent == "sub-sovereign.activate":
            return self._adjudicate_child_activation(request)
        if intent == "A330.certified-update":
            return await self._adjudicate_A330_certified_update(request)

        return refusal_outcome("UNKNOWN_INTENT", self.verified_basis("A301", "A322"))

    # ------------------------------------------------------------------
    # A334 registry-driven dispatch
    # ------------------------------------------------------------------

    def _adjudicate_sync_dispatch(
        self, request: SovereignRequest, child_id: str
    ) -> SovereignOutcome:
        """Adjudicate a sync intent against the A334 hierarchy registry.

        Fail-closed: the child must be a codex-registered child of this
        sovereign; the returned outcome carries the registered primary
        domain and the materialization state for the governed executor.
        """
        if not validate_child_parent(child_id, self.sovereign_id):
            return refusal_outcome(
                "NOT_CODEX_CHILD", self.verified_basis("A322", "A334")
            )
        materialized = child_id in self._sub_sovereigns
        return accepted_outcome(
            {
                "sync_type": request.intent,
                "delegated_to": child_id,
                "primary_domain": primary_domain_of(child_id),
                "materialized": materialized,
                "no_decision": True,
                "no_execution": True,
            },
            self.verified_basis("A322", "A301", "A334"),
        )

    def _adjudicate_child_activation(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A334: authorize activation of a specific registered child."""
        child_id = str(request.payload.get("sub_sovereign", ""))
        return self.authorize_child_activation(child_id)

    def dispatch_child_activation(self, child_id: str) -> SovereignOutcome:
        """Synchronous dispatch adjudication used by the governed executor
        before starting a child (same fail-closed contract as
        ``authorize_child_activation`` plus the codex parent assertion)."""
        if parent_of(child_id) != self.sovereign_id:
            return refusal_outcome("NOT_CODEX_PARENT", ("A334",))
        return self.authorize_child_activation(child_id)

    async def _adjudicate_A330_certified_update(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A330: 認證更新執行例外（唯一執行權限）。"""
        update_type = request.payload.get("update_type")
        if update_type not in {"codex", "governance-policy", "directory"}:
            return refusal_outcome("INVALID_A330_UPDATE_TYPE", self.verified_basis("A330"))

        return accepted_outcome(
            {
                "execution_authorized": True,
                "update_type": update_type,
                "exception": "A330-certified-update-execution",
                "verification": "integrity+identity+history+complete-version-seal",
            },
            self.verified_basis("A330", "A87", "A88"),
        )

    # ------------------------------------------------------------------
    # Child registry (unified with SovereignBase._sub_sovereigns, A334)
    # ------------------------------------------------------------------

    @property
    def _sync_sub_sovereigns(self) -> dict[str, Any]:
        """Alias for the unified child registry populated by the governed
        executor (kept for existing callers)."""
        return self._sub_sovereigns

    def register_sync_sub_sovereign(self, name: str, sovereign: Any) -> None:
        self.register_sub_sovereign(name, sovereign)

    def get_sync_sub_sovereign(self, name: str) -> Any | None:
        return self.get_sub_sovereign(name)

    # ------------------------------------------------------------------
    # Status surfaces
    # ------------------------------------------------------------------

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["sync_sub_sovereigns"] = {
            name: sov.live_status() if hasattr(sov, "live_status") else {"role": name}
            for name, sov in self._sub_sovereigns.items()
        }
        return base

    def orchestration_status(self) -> dict[str, Any]:
        children = {
            name: {
                "primary_domain": primary_domain_of(name),
                "started": bool(getattr(sov, "_started", False)),
            }
            for name, sov in self._sub_sovereigns.items()
        }
        return {
            "state": "active" if self._started else "stopped",
            "owner": self.role,
            "authority": "codex-A334",
            "children": children,
            "delegation": "governed-executor-only",
        }


__all__ = ["SynchronizationSovereign"]
