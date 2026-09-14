"""A69 independent result verification — the verifier is never the work step.

法典依據:
- A69: VERIFY: independent-from-work-step;
  FORBID: work-step-self-verify + executor result accepted without
  independent verification.
- A297/A5: the decision actor is never the execution actor or the sole
  final verifier.

Every executor result passes through ``IndependentVerifier`` before a
sovereign outcome may be accepted; the verifier id is distinct from every
executor identity and self-verification fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from core_system.codex_decision import SovereignOutcome


VERIFIER_ID = "independent-verifier"
# Execution declarations that do not name a concrete executor: the
# decision layer itself or an already-routed governed executor.
_ROUTE_ONLY_EXECUTION = frozenset(
    {"", "none", "decision-layer", "delegated-to-governed-executor"}
)

VerificationCheck = Callable[[Mapping[str, Any]], str]


@dataclass(frozen=True)
class VerificationVerdict:
    verified: bool
    verifier: str
    reasons: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "verified": self.verified,
            "verifier": self.verifier,
            "reasons": list(self.reasons),
        }


class IndependentVerifier:
    """Contract verifier independent from the executor (A69).

    Default checks never trust a self-declared success: the result must
    carry a codex basis, must not announce its own verification, and any
    concrete execution claim must name the executing actor so the receipt
    ledger can prove who executed and who verified.
    """

    def __init__(self, verifier_id: str = VERIFIER_ID) -> None:
        self.verifier_id = str(verifier_id or VERIFIER_ID)
        self._checks: dict[str, list[VerificationCheck]] = {}

    def register(self, intent: str, check: VerificationCheck) -> None:
        self._checks.setdefault(str(intent), []).append(check)

    def verify(
        self, intent: str, executor_actor: str, outcome: SovereignOutcome
    ) -> VerificationVerdict:
        executor = str(executor_actor or "").strip()
        if executor and executor == self.verifier_id:
            return VerificationVerdict(
                verified=False,
                verifier=self.verifier_id,
                reasons=("SELF_VERIFICATION_FORBIDDEN",),
            )
        payload: Mapping[str, Any] = outcome.result or {}
        checks = [*_default_checks(outcome), *self._checks.get(str(intent), ())]
        reasons = tuple(
            reason for check in checks if (reason := check(payload))
        )
        return VerificationVerdict(
            verified=not reasons,
            verifier=self.verifier_id,
            reasons=reasons,
        )


def _default_checks(outcome: SovereignOutcome) -> tuple[VerificationCheck, ...]:
    def basis_present(_payload: Mapping[str, Any]) -> str:
        return "" if outcome.basis else "DECISION_BASIS_MISSING"

    def execution_attested(payload: Mapping[str, Any]) -> str:
        declared = str(payload.get("execution", "none")).strip()
        if declared in _ROUTE_ONLY_EXECUTION:
            return ""
        actor = str(payload.get("execution_actor", "")).strip()
        return "" if actor else "EXECUTION_ACTOR_NOT_ATTESTED"

    def no_self_declared_verification(payload: Mapping[str, Any]) -> str:
        if payload.get("verified") is True or payload.get("verified_by"):
            return "EXECUTOR_SELF_DECLARED_VERIFICATION"
        return ""

    def refusal_consistent(payload: Mapping[str, Any]) -> str:
        if not outcome.accepted and outcome.refusal is None:
            return "REFUSAL_WITHOUT_REASON"
        return ""

    return (
        basis_present,
        execution_attested,
        no_self_declared_verification,
        refusal_consistent,
    )


__all__ = ["IndependentVerifier", "VERIFIER_ID", "VerificationVerdict"]
