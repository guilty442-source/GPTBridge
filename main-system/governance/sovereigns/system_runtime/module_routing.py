"""System Runtime Sovereign — Module Routing (A334)."""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome


class SystemRuntimeModuleRoutingMixin:
    """Module routing to assigned sub-sovereign."""

    _sub_sovereigns: dict[str, Any]
    app: Any

    async def _adjudicate_module_route(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Route module-level operations to assigned sub-sovereign (A334)."""
        module = request.payload.get("module")
        if not module:
            return refusal_outcome("MISSING_MODULE", self.verified_basis("A334"))

        from governance.registries import module_assignment

        assignment = module_assignment(module)
        if not assignment:
            return refusal_outcome("MODULE_UNASSIGNED", self.verified_basis("A334"))

        child_id = assignment.sub_sovereign
        if child_id not in self._sub_sovereigns:
            return refusal_outcome(
                "SUB_SOVEREIGN_NOT_MATERIALIZED", self.verified_basis("A334")
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