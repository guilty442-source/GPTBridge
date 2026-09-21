"""Five-sovereign audit runner for staged Codex amendment requests (G69).

This is the executable adapter between a lifecycle-locked request and the
existing ``CodexAmendmentAuditGate``.  It supplies no audit evidence itself:
each of the five sovereigns must provide an independent check callable.  The
runner only verifies that the request has a built successor, invokes the
existing fail-closed gate, records the lifecycle transition and returns the
gate result for the governed executor.

It never fabricates receipts, never writes the Codex, never seals, signs or
publishes.  A missing callable, an unsuccessful audit, an unrecordable audit
ledger or an unavailable lifecycle record is a denial.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from governance_rule.execution.codex_amendment_audit_gate import (
    AmendmentAuditResult,
    CodexAmendmentAuditGate,
)
from governance_rule.execution.codex_amendment_lifecycle import (
    AmendmentLifecycleError,
    CodexAmendmentRequestLedger,
    STATE_AUDITING,
    STATE_AUDIT_PASSED,
    STATE_READY_FOR_GOVERNOR,
    STATE_REJECTED,
    STATE_SUCCESSOR_BUILT,
    load_amendment_request,
)


class AmendmentAuditRunnerError(RuntimeError):
    """Fail-closed audit runner denial."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}:{detail}" if detail else code)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AmendmentAuditRun:
    ok: bool
    request_id: str
    state: str
    result: AmendmentAuditResult | None
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        record = (
            self.result.to_record()
            if self.result is not None
            else {
                "amendment_id": self.request_id,
                "ok": False,
                "reason": self.error or "AUDIT_RUN_DENIED",
            }
        )
        record["runner"] = {
            "request_id": self.request_id,
            "state": self.state,
            "error": self.error,
        }
        return record


async def run_five_sovereign_audit(
    request_path: str | Path,
    checks: Mapping[str, Callable[[], Any]],
    *,
    ledger: CodexAmendmentRequestLedger,
    gate: CodexAmendmentAuditGate | None = None,
    predecessor: Mapping[str, Any] | None = None,
    requester: str | None = None,
) -> AmendmentAuditRun:
    """Run the existing five-sovereign gate for one built successor request."""
    request_id = ""
    try:
        request = load_amendment_request(request_path)
        request_id = request.request_id
        record = ledger.load_record(request_id)
        if record is None:
            raise AmendmentAuditRunnerError("REQUEST_RECORD_REQUIRED")
        if str(record.get("request_hash")) != request.request_hash:
            raise AmendmentAuditRunnerError("REQUEST_HASH_MISMATCH")
        if str(record.get("state")) != STATE_SUCCESSOR_BUILT:
            raise AmendmentAuditRunnerError(
                "SUCCESSOR_NOT_BUILT", str(record.get("state") or "missing")
            )
        if not checks:
            raise AmendmentAuditRunnerError("SOVEREIGN_CHECKS_REQUIRED")
        audit_gate = gate or CodexAmendmentAuditGate()
        ledger.transition(request_id, STATE_AUDITING)
        result = await audit_gate.audit(
            request_id,
            checks,
            predecessor=predecessor or request.predecessor,
            requester=str(requester or ""),
        )
        if not result.ok:
            ledger.transition(
                request_id,
                STATE_REJECTED,
                evidence={
                    "audit_reason": result.reason,
                    "failed": list(result.failed),
                    "missing": list(result.missing),
                },
            )
            return AmendmentAuditRun(
                False, request_id, STATE_REJECTED, result, result.reason
            )
        ledger.transition(
            request_id,
            STATE_AUDIT_PASSED,
            evidence={
                "audit_reason": result.reason,
                "certificate_hash": str(
                    (result.certificate or {}).get("certificate_hash") or ""
                ),
            },
        )
        ledger.transition(
            request_id,
            STATE_READY_FOR_GOVERNOR,
            evidence={"division_plan_released": result.division_plan is not None},
        )
        return AmendmentAuditRun(
            True, request_id, STATE_READY_FOR_GOVERNOR, result
        )
    except (AmendmentLifecycleError, AmendmentAuditRunnerError) as error:
        if request_id:
            try:
                record = ledger.load_record(request_id)
                if record and record.get("state") not in {
                    STATE_REJECTED,
                    "executed",
                    "withdrawn",
                }:
                    ledger.transition(
                        request_id,
                        STATE_REJECTED,
                        evidence={"error": str(error)},
                    )
            except AmendmentLifecycleError:
                pass
        return AmendmentAuditRun(False, request_id, STATE_REJECTED, None, str(error))


__all__ = [
    "AmendmentAuditRun",
    "AmendmentAuditRunnerError",
    "run_five_sovereign_audit",
]
