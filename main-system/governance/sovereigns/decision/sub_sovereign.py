"""Decision Sovereign — Sub-Sovereign Assignment (A64/A323/A334).

Codex-driven child assignment with explicit action allowlist.
"""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome


class DecisionSubSovereignAssignMixin:
    """Sub-sovereign assignment and coordination."""

    async def _adjudicate_sub_sovereign_assign(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A64/A323: child sub-sovereign assignment."""
        sub_sovereign = request.payload.get("sub_sovereign")
        action = request.payload.get("action", "start")

        from governance.registries import children_of, parent_of

        if sub_sovereign not in children_of("decision-sovereign"):
            return refusal_outcome("UNKNOWN_SUB_SOVEREIGN", self.verified_basis("A130", "A334"))

        valid_actions = {"start", "stop", "coordinate", "assign", "status"}
        if action not in valid_actions:
            return refusal_outcome(
                "INVALID_ACTION",
                self.verified_basis("A10", "A130"),
            )

        return accepted_outcome(
            {
                "sub_sovereign": sub_sovereign,
                "action": action,
                "authority": f"parent-{parent_of(sub_sovereign)}",
                "execution": "delegated-to-governed-executor",
            },
            self.verified_basis("A130", "A284", "A287", "A323", "A334"),
        )

    async def _adjudicate_governance_coordination(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """Governance rule coordination (A63)."""
        edicts = self.edicts()
        children = sorted(self._sub_sovereigns.keys())
        return accepted_outcome(
            {
                "coordination": "governance-rules-aligned",
                "source": "codex-only",
                "edict_count": len(edicts),
                "children": children,
                "children_started": sum(
                    1
                    for c in self._sub_sovereigns.values()
                    if getattr(c, "started", False)
                ),
            },
            self.verified_basis("A12", "A128"),
        )