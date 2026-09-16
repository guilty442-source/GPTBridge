"""Reconcile worker for operations parked in REQUIRES_RECONCILE.

The worker owns no compensation logic.  It claims parked operations under
the same lease discipline as the saga runtime, invokes the injected reconcile
callback (the business authority), and applies the callback's verdict through
the declared state machine:

  * ``RESOLVED``    -> RUNNING -> COMPLETED (receipt + events recorded),
  * ``RETRY``       -> RUNNING (the saga can resume from its checkpoint),
  * ``FAILED``      -> FAILED,
  * ``QUARANTINED`` -> QUARANTINED,
  * ``PENDING``     -> stays parked, lease released for the next cycle.

Fail-closed behaviour: a callback that raises leaves the operation parked
(never faked as resolved); an unrecognised verdict quarantines it; an
operation that cannot honour the transition stays parked with the reason
recorded in its checkpoint receipt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Protocol

from .operation import Operation, OperationStateError
from .persistence import SagaStore
from .types import OperationStatus


class ReconcileDecision(Enum):
    RESOLVED = "RESOLVED"
    RETRY = "RETRY"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"
    PENDING = "PENDING"


@dataclass(frozen=True)
class ReconcileVerdict:
    decision: ReconcileDecision
    detail: str = ""


class ReconcileVerdictError(ValueError):
    """Raised when a reconcile callback returns an unrecognised verdict."""


class ReconcileCallback(Protocol):
    def __call__(self, operation: Operation) -> ReconcileVerdict: ...


@dataclass
class ReconcileReceipt:
    operation: Operation | None
    claimed: bool
    decision: ReconcileDecision | None = None
    reason: str = ""
    detail: str = ""


class ReconcileWorker:
    def __init__(
        self,
        store: SagaStore,
        *,
        worker: str,
        lease_seconds: float = 120.0,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if not worker:
            raise ValueError("WORKER_ID_REQUIRED")
        if lease_seconds <= 0:
            raise ValueError("LEASE_SECONDS_INVALID")
        self.store = store
        self.worker = worker
        self.lease_seconds = lease_seconds
        self.clock = clock or time.time

    def run_once(self, reconcile: ReconcileCallback) -> ReconcileReceipt:
        operation = self.store.claim_next_reconcile(
            worker=self.worker, lease_seconds=self.lease_seconds
        )
        if operation is None:
            return ReconcileReceipt(operation=None, claimed=False, reason="NO_RECONCILE_PENDING")

        step_id = operation.current_step
        self.store.record_event(
            operation.operation_id,
            "compensation_started",
            step_id=step_id,
            detail={"phase": "reconcile", "worker": self.worker},
        )

        try:
            raw = reconcile(operation)
        except Exception as error:  # noqa: BLE001 - stay parked, never fake success
            reason = f"RECONCILE_CALLBACK_ERROR:{type(error).__name__}"
            detail = f"{type(error).__name__}: {error}"
            self._record_receipt(operation, ReconcileDecision.PENDING, reason, detail)
            self.store.record_event(
                operation.operation_id,
                "step_failed",
                step_id=step_id,
                detail={"phase": "reconcile", "reason": reason},
            )
            return ReconcileReceipt(
                operation,
                claimed=True,
                decision=ReconcileDecision.PENDING,
                reason=reason,
                detail=detail,
            )

        try:
            verdict = _coerce_verdict(raw)
        except ReconcileVerdictError as error:
            self._apply_verdict(
                operation,
                ReconcileVerdict(ReconcileDecision.QUARANTINED),
                reason="RECONCILE_VERDICT_INVALID",
                detail=str(error),
            )
            return ReconcileReceipt(
                operation,
                claimed=True,
                decision=ReconcileDecision.QUARANTINED,
                reason="RECONCILE_VERDICT_INVALID",
                detail=str(error),
            )

        return self._apply_verdict(operation, verdict, reason="", detail=verdict.detail)

    def drain(self, reconcile: ReconcileCallback, *, limit: int = 100) -> list[ReconcileReceipt]:
        receipts: list[ReconcileReceipt] = []
        for _ in range(max(1, int(limit))):
            receipt = self.run_once(reconcile)
            if not receipt.claimed:
                break
            receipts.append(receipt)
            if receipt.decision is ReconcileDecision.PENDING:
                break
        return receipts

    # -- internals ----------------------------------------------------------

    def _apply_verdict(
        self,
        operation: Operation,
        verdict: ReconcileVerdict,
        *,
        reason: str,
        detail: str,
    ) -> ReconcileReceipt:
        now = self.clock()
        decision = verdict.decision
        try:
            if decision is ReconcileDecision.RESOLVED:
                operation.transition(OperationStatus.RUNNING, now=now)
                operation.transition(OperationStatus.COMPLETED, now=now)
            elif decision is ReconcileDecision.RETRY:
                operation.transition(OperationStatus.RUNNING, now=now)
            elif decision is ReconcileDecision.FAILED:
                operation.transition(OperationStatus.FAILED, now=now)
            elif decision is ReconcileDecision.QUARANTINED:
                operation.transition(OperationStatus.QUARANTINED, now=now)
            else:
                reason = reason or "RECONCILE_PENDING"
        except OperationStateError as error:
            reason = f"RECONCILE_STATE_INVALID:{error}"

        self._record_receipt(operation, decision, reason, detail)
        operation_id = operation.operation_id
        step_id = operation.current_step
        if decision is ReconcileDecision.RESOLVED and reason == "":
            self.store.record_event(
                operation_id,
                "compensation_completed",
                step_id=step_id,
                detail={"phase": "reconcile", "decision": decision.value},
            )
            self.store.record_event(
                operation_id,
                "operation_completed",
                step_id=step_id,
                detail={"phase": "reconcile"},
            )
        elif decision is ReconcileDecision.QUARANTINED:
            self.store.record_event(
                operation_id,
                "operation_quarantined",
                step_id=step_id,
                detail={"phase": "reconcile", "reason": reason or detail},
            )
        else:
            self.store.record_event(
                operation_id,
                "step_failed",
                step_id=step_id,
                detail={"phase": "reconcile", "decision": decision.value, "reason": reason},
            )
        return ReconcileReceipt(
            operation, claimed=True, decision=decision, reason=reason, detail=detail
        )

    def _record_receipt(
        self,
        operation: Operation,
        decision: ReconcileDecision,
        reason: str,
        detail: str,
    ) -> dict[str, object]:
        """Persist the receipt and release the lease; never fake an outcome."""
        receipt = {
            "worker": self.worker,
            "at": self.clock(),
            "decision": decision.value,
            "reason": reason,
            "detail": detail,
        }
        operation.checkpoint["reconcile"] = receipt
        operation.lease.release()
        self.store.save_operation(operation)
        return receipt


def _coerce_verdict(value: object) -> ReconcileVerdict:
    if isinstance(value, ReconcileVerdict):
        return value
    if isinstance(value, ReconcileDecision):
        return ReconcileVerdict(value)
    raise ReconcileVerdictError(f"RECONCILE_VERDICT_INVALID:{type(value).__name__}")


__all__ = [
    "ReconcileCallback",
    "ReconcileDecision",
    "ReconcileReceipt",
    "ReconcileVerdict",
    "ReconcileVerdictError",
    "ReconcileWorker",
]
