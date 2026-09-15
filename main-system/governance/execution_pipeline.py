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

import time
import uuid
from dataclasses import dataclass
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

# Pre-computed constants to avoid allocation in hot path
_SCHEMA_V1 = "gptbridge.sovereign-outcome/v1"
_TIER_ORDER_LEN = 6  # len(TIER_ORDER)


@dataclass(slots=True)
class _PipelineMetrics:
    """Latency metrics for a single pipeline execution."""
    start_ns: int = 0
    intake_ns: int = 0
    auth_gate_ns: int = 0
    adjudicate_ns: int = 0
    planning_ns: int = 0
    execution_ns: int = 0
    verification_ns: int = 0
    audit_ns: int = 0
    finalize_ns: int = 0

    def to_dict(self) -> dict[str, float]:
        """Return latencies in milliseconds."""
        def ms(ns: int) -> float:
            return ns / 1_000_000.0
        return {
            "total_ms": ms(self.finalize_ns - self.start_ns) if self.finalize_ns else 0.0,
            "intake_ms": ms(self.intake_ns - self.start_ns) if self.intake_ns else 0.0,
            "auth_gate_ms": ms(self.auth_gate_ns - self.intake_ns) if self.auth_gate_ns and self.intake_ns else 0.0,
            "adjudicate_ms": ms(self.adjudicate_ns - self.auth_gate_ns) if self.adjudicate_ns and self.auth_gate_ns else 0.0,
            "planning_ms": ms(self.planning_ns - self.adjudicate_ns) if self.planning_ns and self.adjudicate_ns else 0.0,
            "execution_ms": ms(self.execution_ns - self.planning_ns) if self.execution_ns and self.planning_ns else 0.0,
            "verification_ms": ms(self.verification_ns - self.execution_ns) if self.verification_ns and self.execution_ns else 0.0,
            "audit_ms": ms(self.audit_ns - self.verification_ns) if self.audit_ns and self.verification_ns else 0.0,
            "finalize_ms": ms(self.finalize_ns - self.audit_ns) if self.finalize_ns and self.audit_ns else 0.0,
        }


class SovereignExecutionPipeline:
    """Run one request through all A446 tiers; success requires every receipt."""

    def __init__(self, sovereign: Any) -> None:
        self._sovereign = sovereign
        self._metrics: _PipelineMetrics | None = None

    async def run(self, request: SovereignRequest) -> SovereignOutcome:
        m = _PipelineMetrics(start_ns=time.monotonic_ns())
        self._metrics = m

        ledger = ExecutionReceiptLedger(_request_id(request), request.requester)
        ledger.record(
            ExecutionTier.DISPATCH_INTAKE,
            request.requester,
            "intake",
            {"intent": request.intent, "subject": request.subject},
        )
        m.intake_ns = time.monotonic_ns()

        gate, gate_details = await self._authorization_gate(request)
        m.auth_gate_ns = time.monotonic_ns()

        ledger.record(
            ExecutionTier.AUTHORIZATION_GATE,
            self._sovereign.sovereign_id,
            gate,
            gate_details,
        )
        if gate != "allowed":
            # Refused commands still carry the full tier trail: planning and
            # executor tiers record the refusal instead of being skipped.
            return await self._gate_refusal(ledger, request, gate)
        decision = await self._sovereign._adjudicate(request)
        m.adjudicate_ns = time.monotonic_ns()

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
        m.planning_ns = time.monotonic_ns()

        if decision.accepted:
            outcome = await self._sovereign._delegate_execution(decision, request)
        else:
            outcome = decision
        m.execution_ns = time.monotonic_ns()

        ledger.record(
            ExecutionTier.SPECIALIZED_EXECUTOR,
            _executor_actor(outcome),
            "executed" if decision.accepted else "none",
            {
                "declared": str((outcome.result or {}).get("execution", "none")),
                "accepted": bool(outcome.accepted),
            },
        )
        return await self._finalize(ledger, request, outcome, m)

    async def _gate_refusal(
        self,
        ledger: ExecutionReceiptLedger,
        request: SovereignRequest,
        gate: str,
    ) -> SovereignOutcome:
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

    async def _authorization_gate(
        self, request: SovereignRequest
    ) -> tuple[str, dict[str, Any]]:
        # CONCURRENCY: requester identity and intent allowlist are independent
        # domain checks — they run on the parallel core and join fail-closed.
        from .parallel_core import (
            AUTHORIZATION_GATE_DEADLINE_SECONDS,
            DomainCheck,
            SovereignParallelCore,
        )

        core = SovereignParallelCore(
            label="authorization-gate",
            deadline=AUTHORIZATION_GATE_DEADLINE_SECONDS,
        )
        run = await core.run_stage(
            (
                DomainCheck(
                    "requester",
                    lambda: self._sovereign._verify_requester(request),
                ),
                DomainCheck(
                    "intent",
                    lambda: self._sovereign._verify_intent(request.intent),
                ),
            )
        )
        details: dict[str, Any] = {"parallel_core": run.summary()}
        requester = run.verdict("requester")
        if requester is None or requester.error:
            details["error"] = requester.error if requester else "check-missing"
            return "AUTHORIZATION_GATE_ERROR", details
        if not requester.ok:
            return "UNAUTHORIZED_REQUESTER", details
        intent = run.verdict("intent")
        if intent is None or intent.error:
            details["error"] = intent.error if intent else "check-missing"
            return "AUTHORIZATION_GATE_ERROR", details
        if not intent.ok:
            return "UNAUTHORIZED_INTENT", details
        return "allowed", details

    async def _finalize(
        self,
        ledger: ExecutionReceiptLedger,
        request: SovereignRequest,
        outcome: SovereignOutcome,
        metrics: _PipelineMetrics | None = None,
    ) -> SovereignOutcome:
        v_start = time.monotonic_ns()
        verdict = self._sovereign.verify_execution_result(
            request.intent, ledger.executor_actor, outcome
        )
        if metrics:
            metrics.verification_ns = time.monotonic_ns()

        verdict_record = verdict.to_record()  # Cache to avoid repeated calls
        ledger.record(
            ExecutionTier.RESULT_VERIFICATION,
            verdict.verifier,
            "verified" if verdict.verified else "failed",
            verdict_record,
        )
        if not self._publish_audit_tier(ledger, outcome, verdict):
            return _refusal("AUDIT_PUBLICATION_FAILED", ledger, verdict)
        # A446: require complete receipts, independent verification, and audit publication
        if not outcome.accepted:
            return _with_receipts(outcome, ledger, verdict, verdict_record, metrics)
        if not verdict.verified:
            return _refusal("INDEPENDENT_VERIFICATION_FAILED", ledger, verdict, metrics)
        if not ledger.complete():
            return _refusal("EXECUTION_TIER_INCOMPLETE", ledger, verdict, metrics)

        # Attach verification proof to the result for independent verification traceability
        # Reuse outcome.result dict when possible to avoid extra allocation
        result = outcome.result
        if result is None:
            result = {}
        else:
            # Create a shallow copy only if we need to mutate
            result = dict(result)
        result["execution_receipts"] = ledger.summary()
        result["verification"] = verdict_record
        result["independently_verified"] = True
        result["verifier"] = verdict.verifier
        if metrics:
            metrics.finalize_ns = time.monotonic_ns()
        return SovereignOutcome(
            accepted=outcome.accepted,
            refusal=outcome.refusal,
            result=result,
            basis=outcome.basis,
        )

    def _publish_audit_tier(
        self,
        ledger: ExecutionReceiptLedger,
        outcome: SovereignOutcome,
        verdict: VerificationVerdict,
    ) -> bool:
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
            return True
        except ReceiptError as error:
            ledger.record(
                ExecutionTier.AUDIT_PUBLICATION,
                "audit-ledger",
                "failed",
                {"error": str(error)[:200]},
            )
            return False


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
    verdict_record: dict[str, Any] | None = None,
    metrics: _PipelineMetrics | None = None,
) -> SovereignOutcome:
    result = outcome.result
    if result is None:
        result = {}
    else:
        result = dict(result)
    result["schema"] = _SCHEMA_V1
    result["execution_receipts"] = ledger.summary()
    result["verification"] = verdict_record if verdict_record is not None else verdict.to_record()
    if metrics:
        metrics.finalize_ns = time.monotonic_ns()
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
    metrics: _PipelineMetrics | None = None,
) -> SovereignOutcome:
    tokens = _GATE_BASIS.get(reason, ("A446", "A121"))
    basis = verified_basis(tokens)
    result: dict[str, Any] = {"schema": _SCHEMA_V1}
    if ledger is not None:
        result["execution_receipts"] = ledger.summary()
    if verdict is not None:
        result["verification"] = verdict.to_record()
    if metrics:
        metrics.finalize_ns = time.monotonic_ns()
    return SovereignOutcome(
        accepted=False,
        refusal=Refusal(reason, basis.references),
        result=result,
        basis=basis.references,
    )


__all__ = ["SovereignExecutionPipeline"]
