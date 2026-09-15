"""Sovereign Delegation Mixin — inter-sovereign coordination via A334 routed delegation."""

from __future__ import annotations

from typing import Any

from core_system.codex_decision import (
    SovereignOutcome,
    refusal_outcome,
)

from .._delegation import (
    _stamp_delegation,
    _attach_target_receipt,
)


class DelegationMixin:
    """Mixin providing inter-sovereign coordination via A334 routed delegation."""

    @property
    def sovereign_id(self) -> str:
        raise NotImplementedError("Subclass must implement 'sovereign_id' property")

    @property
    def app(self) -> Any:
        raise NotImplementedError("Subclass must implement 'app' property")

    def resolve_sovereign(self, sovereign_id: str) -> Any | None:
        """Resolve another sovereign instance via the A334 hierarchy."""
        from ...registries import resolve_sovereign

        return resolve_sovereign(self.app, sovereign_id)

    async def delegate_to(
        self, target_sovereign_id: str, request: Any
    ) -> SovereignOutcome:
        """Route a request through the target sovereign's single entry gate.

        The requester is rewritten to this sovereign's identity so the
        target's A10/A11 gates observe the true sovereign origin; a
        sub-sovereign target additionally enforces its A334 single-parent
        check, so only the codex parent can delegate into it.  Fails
        closed when the target is not materialized or not started.

        The returned outcome carries a verifiable ``DelegationReceipt`` in
        ``result["delegation_receipt"]`` with the target's execution
        receipt trail — so callers can verify the delegation actually
        reached the target and was processed, not merely declared.
        """
        target = self.resolve_sovereign(target_sovereign_id)
        if target is None:
            return refusal_outcome("TARGET_SOVEREIGN_UNAVAILABLE", ("A334",))
        if not getattr(target, "started", False):
            return refusal_outcome(
                "TARGET_SOVEREIGN_NOT_STARTED", ("A10", "A11")
            )
        forwarded = _stamp_delegation(
            request, self.sovereign_id, target_sovereign_id
        )
        outcome = await target.handle(forwarded)
        return _attach_target_receipt(
            outcome, request, self.sovereign_id, target_sovereign_id
        )


__all__ = ["DelegationMixin"]