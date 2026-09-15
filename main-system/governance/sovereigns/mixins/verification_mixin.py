"""Sovereign Verification Mixin — independent verification (A446)."""

from __future__ import annotations

from typing import Any

from governance.independent_verifier import IndependentVerifier, VerificationVerdict


class VerificationBase:
    """Mixin providing independent verification (A446)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # A446 independent verifier — never the work step; domain checks may
        # be registered by subclasses via ``register_verification_check``.
        self._independent_verifier = IndependentVerifier()


    def register_verification_check(self, intent: str, check: Any) -> None:
        """Register an independent domain check for ``intent`` (A446)."""
        self._independent_verifier.register(intent, check)

    def _governance(self) -> Any:
        """The app governance service, or None when unavailable (A436/A10).

        Shared accessor so permission/auth mixins never call an undefined
        method: the explicit ``_governance_ref`` wins, then ``app.governance``.
        """
        ref = getattr(self, "_governance_ref", None)
        if ref is not None:
            return ref
        return getattr(self.app, "governance", None)

    def verify_execution_result(
        self, intent: str, executor_actor: str, outcome: Any
    ) -> VerificationVerdict:
        """Independent verification of an executor result (A446/A121)."""
        return self._independent_verifier.verify(intent, executor_actor, outcome)


__all__ = ["VerificationBase"]