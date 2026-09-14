"""System Runtime Sovereign — A322 Retry/Cancel and Convergence."""

from __future__ import annotations

from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome


class SystemRuntimeA322DecisionMixin:
    """A322-style retry/cancel, convergence acceptance, conflict isolation."""

    _sub_sovereigns: dict[str, Any]
    app: Any

    async def _adjudicate_retry_cancel(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: adjudicate retry or cancel for failed runtime operation."""
        child_id = request.payload.get("child_id")
        if not child_id:
            return refusal_outcome("MISSING_CHILD_ID", self.verified_basis("A322"))

        attempts = int(request.payload.get("attempts", 0))
        max_attempts = int(request.payload.get("max_attempts", 3))

        if attempts >= max_attempts:
            return accepted_outcome(
                {
                    "decision": "cancel",
                    "child_id": child_id,
                    "reason": "max-attempts-exceeded",
                },
                self.verified_basis("A322"),
            )
        else:
            return accepted_outcome(
                {
                    "decision": "retry",
                    "child_id": child_id,
                    "attempt": attempts + 1,
                },
                self.verified_basis("A322"),
            )

    async def _adjudicate_convergence_acceptance(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: adjudicate convergence acceptance."""
        results = request.payload.get("results", [])
        if not isinstance(results, list):
            return refusal_outcome("INVALID_RESULTS", self.verified_basis("A322"))

        all_converged = all(
            isinstance(r, dict) and r.get("converged", False)
            for r in results
        )

        return accepted_outcome(
            {
                "decision": "convergence-acceptance",
                "accepted": all_converged,
                "results": results,
            },
            self.verified_basis("A322"),
        )

    async def _adjudicate_conflict_isolation(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A322: adjudicate conflict isolation."""
        return accepted_outcome(
            {
                "decision": "conflict-isolation",
                "disposition": "sequential-serialized",
                "rationale": "A322 atomic boundary + conflict isolation",
            },
            self.verified_basis("A322"),
        )