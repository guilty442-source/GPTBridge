"""Decision Sovereign — Repair Decision Chain (A152/A154/E127/E128).

Owns the repair decision; routes through permission validation → governed
executor → verification. Does not execute repairs directly (A63/A64).
"""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome
from core_system.repair_decision_chain import RepairDecisionChain


class DecisionRepairMixin:
    """Repair decision and routing (A152/A154)."""

    _repair_decision_chain: RepairDecisionChain

    async def _adjudicate_repair_decision(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A152/A154/E127/E128: repair decision chain."""
        classified_signal = request.payload.get("classified_signal")
        if not classified_signal:
            return refusal_outcome("MISSING_CLASSIFIED_SIGNAL", self.verified_basis("A152"))

        if not isinstance(classified_signal, dict):
            return refusal_outcome(
                "INVALID_CLASSIFIED_SIGNAL",
                self.verified_basis("A152", "A154"),
            )

        try:
            result = self._repair_decision_chain.decide_and_route(classified_signal)
        except Exception as error:
            return refusal_outcome(
                "REPAIR_CHAIN_ERROR",
                self.verified_basis("A152", "E128"),
            )

        if not isinstance(result, dict):
            return refusal_outcome(
                "REPAIR_CHAIN_INVALID_RESULT",
                self.verified_basis("A152"),
            )

        if not result.get("authorized", False):
            reason = result.get("reason", "REPAIR_NOT_AUTHORIZED")
            return refusal_outcome(
                reason,
                self.verified_basis("A152", "A154", "E128"),
            )

        return accepted_outcome(
            {
                "repair_decision": "authorized",
                "repair_type": classified_signal.get("repair_type", "unknown"),
                "route": "permission-validation > governed-executor > verification",
                "chain_result": result,
                "forbidden": "maintenance-owning-non-health-decisions",
            },
            self.verified_basis("A152", "A154", "E127", "E128"),
        )

    def decide_and_route_repair(
        self,
        classified_signal: dict[str, Any],
        *,
        user_confirmed: bool = False,
    ) -> dict[str, Any]:
        """A152 repair-decision entry point."""
        return self._repair_decision_chain.decide_and_route(
            classified_signal, user_confirmed=user_confirmed
        )