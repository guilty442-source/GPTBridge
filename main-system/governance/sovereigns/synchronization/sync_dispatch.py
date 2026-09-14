"""Synchronization Sovereign — Sync Dispatch Adjudication (A301/A334).

Adjudicates sync coordination intents and dispatches to appropriate
sub-sovereigns. Decision-only; execution delegated.
"""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome, verified_basis


class SyncDispatchMixin:
    """Sync coordination dispatch adjudication."""

    _sub_sovereigns: dict[str, Any]
    app: Any

    async def _adjudicate_sync_dispatch(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A301: adjudicate sync coordination intent."""
        child_id, child = self._resolve_sync_child(request.intent)
        if child_id is None or child is None:
            return refusal_outcome(
                "UNKNOWN_SYNC_INTENT", verified_basis(("A301", "A334"))
            )

        # Delegate to the sub-sovereign through the governed executor
        # (this sovereign adjudicates; executor executes)
        return await self.delegate_to(
            child_id,
            SovereignRequest(
                intent=request.intent,
                subject=request.subject,
                requester=request.requester,
                payload=request.payload,
            ),
        )

    async def _adjudicate_module_routing(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Route module-level operations to assigned sub-sovereign (A334)."""
        module = request.payload.get("module")
        if not module:
            return refusal_outcome("MISSING_MODULE", verified_basis(("A334",)))

        from governance.registries import module_assignment

        assignment = module_assignment(module)
        if not assignment:
            return refusal_outcome("MODULE_UNASSIGNED", verified_basis(("A334",)))

        child_id = str(
            assignment.get("managing_sub_sovereign")
            or assignment.get("sub_sovereign")
            or ""
        )
        if not child_id:
            return refusal_outcome(
                "SUB_SOVEREIGN_UNASSIGNED", verified_basis(("A334",))
            )
        if child_id not in self._sub_sovereigns:
            return refusal_outcome(
                "SUB_SOVEREIGN_NOT_MATERIALIZED", verified_basis(("A334",))
            )

        return await self.delegate_to(
            child_id,
            SovereignRequest(
                intent=request.intent,
                subject=request.subject,
                requester=request.requester,
                payload=request.payload,
            ),
        )