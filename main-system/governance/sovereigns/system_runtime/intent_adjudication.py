"""System Runtime Sovereign — Intent Adjudication."""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome


class SystemRuntimeIntentMixin:
    """Runtime status, action, sub-sovereign management, health coordination."""

    _sub_sovereigns: dict[str, Any]
    app: Any
    _runtime_state: str

    async def _adjudicate_runtime_status(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Runtime status query."""
        return accepted_outcome(
            {
                "runtime_state": self._runtime_state,
                "sub_sovereigns": list(self._sub_sovereigns.keys()),
            },
            self.verified_basis("A28"),
        )

    async def _adjudicate_runtime_action(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Runtime action adjudication — decision-only."""
        action = request.payload.get("action")
        if not action:
            return refusal_outcome("MISSING_ACTION", self.verified_basis("A28"))

        # Route to appropriate sub-sovereign or decision-sovereign
        from governance.registries import children_of

        for child_id in children_of("system-runtime-sovereign"):
            if action in str(primary_domain_of(child_id)):
                return await self.delegate_to(
                    child_id,
                    SovereignRequest(
                        intent=request.intent,
                        subject=request.subject,
                        requester=request.requester,
                        payload=request.payload,
                    ),
                )

        return accepted_outcome(
            {"action": action, "routed": "decision-sovereign"},
            self.verified_basis("A28"),
        )

    async def _adjudicate_sub_sovereign_manage(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Sub-sovereign management — coordinate with decision-sovereign."""
        return accepted_outcome(
            {"management": "coordinated-via-decision-sovereign"},
            self.verified_basis("A28", "A334"),
        )

    async def _adjudicate_health_coordinate(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Health coordination with health-maintenance-test-sub-sovereign."""
        child_id, child = self._resolve_runtime_child("runtime.health")
        if child_id and child:
            return await self.delegate_to(
                child_id,
                SovereignRequest(
                    intent=request.intent,
                    subject=request.subject,
                    requester=request.requester,
                    payload=request.payload,
                ),
            )
        return accepted_outcome(
            {"health_coordination": "no-health-child"},
            self.verified_basis("A28"),
        )

    def _resolve_runtime_child(self, intent: str) -> tuple[str | None, Any | None]:
        intent_map = {
            "runtime.readiness": "startup-sub-sovereign",
            "runtime.health": "health-maintenance-test-sub-sovereign",
        }
        child_id = intent_map.get(intent)
        if child_id is None:
            return None, None
        from governance.registries import validate_child_parent
        if not validate_child_parent(child_id, self.sovereign_id):
            return None, None
        child = getattr(self, "_sub_sovereigns", {}).get(child_id)
        return child_id, child