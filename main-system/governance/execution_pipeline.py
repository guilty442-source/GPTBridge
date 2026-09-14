"""A446 execution pipeline — every sovereign request runs all six tiers.

法典依據:
- A446: EXECUTION-LAYER-TIERS: dispatch-intake > authorization-and-governance-gate
  > task-planning > specialized-executor > result-verification
  > state-event-audit-publication; FORBID: tier-skip +
  executor-self-authorize + executor-self-dispatch + work-step-self-verify +
  unrecorded-result.
- A121: pre-execution-verify + post-execution-audit + violation-stop-record.
- A10/A11: explicit allowlist, fail-closed.

An executor result is never accepted on its own claim: acceptance requires
the independent-verifier receipt, the audit-election receipt and a complete
ledger.  Any missing tier, failed verification or failed audit publication
returns a refusal instead.
"""

from __future__ import annotations

import uuid
from typing import Any, Mapping

from core_system.codex_decision import (
    Refusal,
    SovereignOutcome,
    SovereignRequest,
    verified_basis,
)

from .execution_receipts import (
    ExecutionReceiptLedger,
    ExecutionTier,
    ReceiptError,
    publish_execution_audit,
)
from .independent_verifier import VerificationVerdict

_AUTHORIZATION_ERRORS = (
    OSError,
    ValueError,
    KeyError,
    RuntimeError,
    ImportError,
    PermissionError,
)
_ROUTE_ONLY_EXECUTION = frozenset(
    {"", "none", "decision-layer", "delegated-to-governed-executor"}
)
_GATE_BASIS: Mapping[str, tuple[str, ...]] = {
    "UNAUTHORIZED_REQUESTER": ("A10", "A11", "A446"),
    "UNAUTHORIZED_INTENT": ("A10", "A12", "A446"),
    "AUTHORIZATION_GATE_ERROR": ("A11", "A446", "A121"),
    "AUDIT_PUBLICATION_FAILED": ("A46", "A446", "A121"),
    "INDEPENDENT_VERIFICATION_FAILED": ("A446", "A121"),
    "EXECUTION_TIER_INCOMPLETE": ("A446", "A121"),
}


class SovereignExecutionPipeline:
    """Run one request through all A446 tiers; success requires every receipt."""

    def __init__(self, sovereign: Any) -> None:
        self._sovereign = sovereign

    async def run(self, request: SovereignRequest) -> SovereignOutcome:
        ledger = ExecutionReceiptLedger(_request_id(request), request.requester)
        ledger.record(
            ExecutionTier.DISPATCH_INTAKE,
            request.requester,
            "intake",
            {"intent": request.intent, "subject": request.subject},
        )
        gate, gate_details = await self._authorization_gate(request)
        ledger.record(
            ExecutionTier.AUTHORIZATION_GATE,
            self._sovereign.sovereign_id,
            gate,
            gate_details,
        )
        if gate != "allowed":
            # Refused commands still carry the full tier trail: planning and
            # executor tiers record the refusal instead of being skipped.
            ledger.record(
                ExecutionTier.TASK_PLANNING,
                self._sovereign.sovereign_id,
                "not-planned",
                {"reason": gate},
            )
            ledger.record(
                ExecutionTier.SPECIALIZED_EXECUTOR,
                "decision-layer",
                "not-executed",
                {"reason": gate},
            )
            return await self._finalize(ledger, request, _refusal(gate))
        decision = await self._sovereign._adjudicate(request)
        plan_details = _plan(decision, request)
        plan_details["decision_basis"] = (
            list(getattr(decision.basis, "references", decision.basis) or ())
        )
        plan_details["refusal_reason"] = decision.refusal.reason_code if decision.refusal else None
        ledger.record(
            ExecutionTier.TASK_PLANNING,
            self._sovereign.sovereign_id,
            "planned",
            plan_details,
        )
        if decision.accepted:
            outcome = await self._sovereign._delegate_execution(decision, request)
        else:
            outcome = decision
        ledger.record(
            ExecutionTier.SPECIALIZED_EXECUTOR,
            _executor_actor(outcome),
            "executed" if decision.accepted else "none",
            {
                "declared": str((outcome.result or {}).get("execution", "none")),
                "accepted": bool(outcome.accepted),
            },
        )
        return await self._finalize(ledger, request, outcome)

    async def _authorization_gate(
        self, request: SovereignRequest
    ) -> tuple[str, dict[str, Any]]:
        try:
            requester_ok = await self._sovereign._verify_requester(request)
        except _AUTHORIZATION_ERRORS as error:
            return "AUTHORIZATION_GATE_ERROR", {
                "error": type(error).__name__,
                "detail": str(error)[:200],
            }
        if not requester_ok:
            return "UNAUTHORIZED_REQUESTER", {}
        if not self._sovereign._verify_intent(request.intent):
            return "UNAUTHORIZED_INTENT", {}
        return "allowed", {}

    async def _finalize(
        self,
        ledger: ExecutionReceiptLedger,
        request: SovereignRequest,
        outcome: SovereignOutcome,
    ) -> SovereignOutcome:
        verdict = self._sovereign.verify_execution_result(
            request.intent, ledger.executor_actor, outcome
        )
        ledger.record(
            ExecutionTier.RESULT_VERIFICATION,
            verdict.verifier,
            "verified" if verdict.verified else "failed",
            verdict.to_record(),
        )
        try:
            entry = publish_execution_audit(
                ledger,
                {"accepted": bool(outcome.accepted), "verification": verdict.to_record()},
            )
            ledger.record(
                ExecutionTier.AUDIT_PUBLICATION,
                "audit-ledger",
                "published",
                {"entry": entry},
            )
        except ReceiptError as error:
            ledger.record(
                ExecutionTier.AUDIT_PUBLICATION,
                "audit-ledger",
                "failed",
                {"error": str(error)[:200]},
            )
            return _refusal("AUDIT_PUBLICATION_FAILED", ledger, verdict)
        # A446: require complete receipts, independent verification, and audit publication
        if not outcome.accepted:
            return _with_receipts(outcome, ledger, verdict)
        if not verdict.verified:
            return _refusal("INDEPENDENT_VERIFICATION_FAILED", ledger, verdict)
        if not ledger.complete():
            return _refusal("EXECUTION_TIER_INCOMPLETE", ledger, verdict)
        # Attach verification proof to the result for independent verification traceability
        result = dict(outcome.result or {})
        result["execution_receipts"] = ledger.summary()
        result["verification"] = verdict.to_record()
        result["independently_verified"] = True
        result["verifier"] = verdict.verifier
        return SovereignOutcome(
            accepted=outcome.accepted,
            refusal=outcome.refusal,
            result=result,
            basis=outcome.basis,
        )


def _request_id(request: SovereignRequest) -> str:
    for key in ("request_id", "_request_id", "task_id"):
        value = request.payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"{request.intent}:{uuid.uuid4().hex[:16]}"


def _plan(decision: SovereignOutcome, request: SovereignRequest) -> dict[str, Any]:
    return {
        "intent": request.intent,
        "subject": request.subject,
        "accepted": bool(decision.accepted),
    }


def _executor_actor(outcome: SovereignOutcome) -> str:
    payload = outcome.result or {}
    actor = str(payload.get("execution_actor", "")).strip()
    if actor:
        return actor
    declared = str(payload.get("execution", "none")).strip()
    if declared in ("", "none", "decision-layer"):
        return "decision-layer"
    if declared == "delegated-to-governed-executor":
        return "governed-executor"
    return "unattested"


def _with_receipts(
    outcome: SovereignOutcome,
    ledger: ExecutionReceiptLedger,
    verdict: VerificationVerdict,
) -> SovereignOutcome:
    result = dict(outcome.result or {})
    result["schema"] = "gptbridge.sovereign-outcome/v1"
    result["execution_receipts"] = ledger.summary()
    result["verification"] = verdict.to_record()
    return SovereignOutcome(
        accepted=outcome.accepted,
        refusal=outcome.refusal,
        result=result,
        basis=outcome.basis,
    )


def _refusal(
    reason: str,
    ledger: ExecutionReceiptLedger | None = None,
    verdict: VerificationVerdict | None = None,
) -> SovereignOutcome:
    tokens = _GATE_BASIS.get(reason, ("A446", "A121"))
    basis = verified_basis(tokens)
    result: dict[str, Any] = {"schema": "gptbridge.sovereign-outcome/v1"}
    if ledger is not None:
        result["execution_receipts"] = ledger.summary()
    if verdict is not None:
        result["verification"] = verdict.to_record()
    return SovereignOutcome(
        accepted=False,
        refusal=Refusal(reason, basis.references),
        result=result,
        basis=basis.references,
    )


__all__ = ["SovereignExecutionPipeline"]
