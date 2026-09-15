"""A446 independent result verification — the verifier is never the work step.

法典依據:
- A446: VERIFY: independent-from-work-step;
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


def _basis_references(basis: Any) -> tuple[str, ...]:
    """Normalize decision basis — ``DecisionBasis`` or a plain tuple."""
    return tuple(getattr(basis, "references", basis))


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
    """Contract verifier independent from the executor (A446).

    Default checks never trust a self-declared success: the result must
    carry a codex basis, must not announce its own verification, and any
    concrete execution claim must name the executing actor so the receipt
    ledger can prove who executed and who verified.

    Caches verification results keyed by (intent, executor_actor, outcome_hash)
    to avoid redundant computation for identical executor claims.
    """

    def __init__(self, verifier_id: str = VERIFIER_ID, *, cache_size: int = 256) -> None:
        self.verifier_id = str(verifier_id or VERIFIER_ID)
        self._checks: dict[str, list[VerificationCheck]] = {}
        self._cache_size = max(1, int(cache_size))
        self._cache: dict[tuple[Any, ...], VerificationVerdict] = {}
        self._cache_hits = 0
        self._cache_misses = 0

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
        custom_checks = self._checks.get(str(intent), ())
        cache_key = self._cache_key(intent, executor, outcome, payload)
        # Custom intent checks may read arbitrary payload fields, so
        # only default-check verdicts are cacheable.
        verdict = self._cache.get(cache_key) if not custom_checks else None
        if verdict is not None:
            self._cache_hits += 1
            return verdict
        self._cache_misses += 1
        verdict = self._verify_uncached(payload, intent, outcome)
        if not custom_checks:
            if len(self._cache) >= self._cache_size:
                self._cache.clear()
            self._cache[cache_key] = verdict
        return verdict

    def _cache_key(
        self,
        intent: str,
        executor: str,
        outcome: SovereignOutcome,
        payload: Mapping[str, Any],
    ) -> tuple[Any, ...]:
        # Covers every field the default checks read: outcome
        # acceptance/basis/refusal plus the payload's execution,
        # execution_actor, verified and verified_by claims.
        return (
            intent,
            executor,
            bool(outcome.accepted),
            _basis_references(outcome.basis) if outcome.basis else (),
            outcome.refusal.reason_code if outcome.refusal else "",
            str(payload.get("execution", "none")).strip(),
            str(payload.get("execution_actor", "")).strip(),
            payload.get("verified") is True,
            bool(payload.get("verified_by")),
        )

    def _verify_uncached(
        self, payload: Mapping[str, Any], intent: str, outcome: SovereignOutcome
    ) -> VerificationVerdict:
        checks = [*_default_checks(outcome), *self._checks.get(str(intent), ())]
        reasons = tuple(
            reason for check in checks if (reason := check(payload))
        )
        return VerificationVerdict(
            verified=not reasons,
            verifier=self.verifier_id,
            reasons=reasons,
        )

    def cache_stats(self) -> dict[str, int]:
        """Return cache hit/miss statistics."""
        return {
            "hits": self._cache_hits,
            "misses": self._cache_misses,
            "size": len(self._cache),
        }

    def clear_cache(self) -> None:
        """Clear the verification cache."""
        self._cache.clear()
        self._cache_hits = 0
        self._cache_misses = 0


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
