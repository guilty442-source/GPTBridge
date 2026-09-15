"""Sovereign Execution Mixin — execution delegation and receipt attachment."""

from __future__ import annotations

from typing import Any

from core_system.codex_decision import (
    SovereignOutcome,
    refusal_outcome,
    verified_basis,
)

from .._delegation import attach_delegation_receipt


class ExecutionBase:
    """Mixin providing execution delegation and receipt attachment."""


    async def _delegate_execution(
        self, decision: SovereignOutcome, request: Any
    ) -> SovereignOutcome:
        """委派执行给受治理执行器（A446/A121）。

        Fail-closed default: a sovereign that does not override this hook
        cannot claim successful execution.  Returning the bare adjudication
        result would mask the absence of execution behind an accepted
        outcome, violating A446 (EXECUTION-LAYER-TIERS requires a real
        specialized-executor step) and A121 (post-execution-audit must
        record an actual execution, not a decision echo).

        Subclasses MUST override this hook to do one of:
          * dispatch the decision to a registered governed executor,
            run independent verification, and record the audit trail; or
          * attest that the adjudication was a pure decision / query with
            no execution side-effect (e.g. permission.query, runtime.status)
            and return the decision unchanged with that attestation recorded.
        """
        return refusal_outcome(
            "EXECUTION_NOT_DELEGATED",
            self.verified_basis("A446", "A121"),
        )

    def _attach_delegation_receipt(
        self,
        decision: SovereignOutcome,
        request: Any,
        execution_mode: str = "decision-only",
    ) -> SovereignOutcome:
        """Mint a verifiable delegation receipt and attach it to the outcome.

        Thin wrapper around ``attach_delegation_receipt`` (A446/A121).
        """
        return attach_delegation_receipt(
            decision, request, self.sovereign_id, execution_mode
        )


__all__ = ["ExecutionBase"]