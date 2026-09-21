"""Production entry for the Saga layer: persist, claim, execute, resume.

``run_operation`` is the wiring point between a step plan + injected handlers
and the durable operation authority: it creates the operation row, claims it
under a lease, executes through ``SagaExecutor`` while persisting every step
and event, and leaves the operation in the declared state machine
(``COMPLETED`` / ``FAILED`` / ``REQUIRES_RECONCILE`` / ``QUARANTINED``).
Missing handlers keep the executor's ``NO_HANDLER`` fail-closed semantics.

Replays are honest: the same idempotency key / fingerprint returns the stored
operation without re-executing handlers, and a foreign active lease is never
stolen.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, Callable

from .operation import Operation
from .persistence import SagaStore
from .saga import (
    SagaExecutor,
    SagaOutcome,
    StepHandler,
    StepHandlerResolver,
)
from .steps import StepPlan, StepSpec
from .types import Engine, OperationStatus, TERMINAL_STATUSES


class SagaRuntimeError(RuntimeError):
    """Raised when ``run_operation`` cannot honour its contract (fail-closed)."""


@dataclass
class RunReceipt:
    operation: Operation
    claimed: bool
    replayed: bool = False
    reason: str = ""
    outcome: SagaOutcome | None = None


def run_operation(
    operation: Operation,
    plan: StepPlan,
    handlers: Mapping[Engine, StepHandler] | StepHandlerResolver,
    *,
    store: SagaStore,
    worker: str,
    executor: SagaExecutor | None = None,
    clock: Callable[[], float] | None = None,
    pg_transaction_open: bool = False,
    sleep: Callable[[float], None] | None = None,
) -> RunReceipt:
    if not operation.operation_id:
        raise SagaRuntimeError("OPERATION_ID_REQUIRED")
    if not worker:
        raise SagaRuntimeError("WORKER_ID_REQUIRED")
    executor = executor or SagaExecutor()
    clock = clock or time.time

    stored, created = store.create_operation(operation)
    if not created:
        if stored.operation_id != operation.operation_id:
            return RunReceipt(stored, claimed=False, replayed=True, reason="IDEMPOTENT_REPLAY")
        if stored.status in TERMINAL_STATUSES:
            return RunReceipt(
                stored,
                claimed=False,
                replayed=True,
                reason=f"TERMINAL_REPLAY:{stored.status.value}",
            )

    claimed = store.claim_operation(
        operation.operation_id, worker=worker, lease_seconds=executor.lease_seconds
    )
    if claimed is None:
        current = store.load_operation(operation.operation_id) or stored
        return RunReceipt(
            current, claimed=False, reason=f"NOT_CLAIMED:{current.status.value}"
        )

    missing = _missing_remaining(claimed, plan, handlers)
    if missing:
        spec = missing[0]
        reason = f"NO_HANDLER:{spec.engine.value}"
        claimed.transition(OperationStatus.QUARANTINED, now=clock())
        claimed.lease.release()
        store.save_operation(claimed)
        store.record_event(
            claimed.operation_id,
            "operation_quarantined",
            step_id=spec.step_id,
            detail={"reason": reason},
        )
        return RunReceipt(claimed, claimed=True, reason=reason)

    # Y6: single commit boundary per observer event when the store
    # supports it (PostgresSagaStore.transaction); other stores keep the
    # per-call semantics via a no-op fallback.
    transaction = getattr(store, "transaction", None)

    def observer(
        event_type: str, target: Operation, spec: StepSpec, detail: dict[str, Any]
    ) -> None:
        with transaction() if callable(transaction) else nullcontext():
            if event_type in ("step_completed", "compensation_started", "compensation_completed"):
                result = target.steps.get(spec.step_id)
                if result is not None:
                    store.save_step(
                        target.operation_id, spec, result, attempt=int(detail.get("attempt", 1))
                    )
            store.save_operation(target)
            store.record_event(target.operation_id, event_type, step_id=spec.step_id, detail=detail)
            if event_type in ("step_started", "step_completed", "step_failed"):
                store.heartbeat(
                    target.operation_id, worker=worker, lease_seconds=executor.lease_seconds
                )

    def heartbeat(target: Operation, spec: StepSpec) -> None:
        store.heartbeat(
            target.operation_id, worker=worker, lease_seconds=executor.lease_seconds
        )

    outcome = executor.run(
        claimed,
        plan,
        handlers,
        now=clock(),
        pg_transaction_open=pg_transaction_open,
        sleep=sleep,
        observer=observer,
        heartbeat=heartbeat,
    )

    if claimed.status in (
        OperationStatus.COMPLETED,
        OperationStatus.FAILED,
        OperationStatus.QUARANTINED,
        OperationStatus.REQUIRES_RECONCILE,
    ):
        claimed.lease.release()
    store.save_operation(claimed)
    if claimed.status is OperationStatus.COMPLETED:
        store.record_event(
            claimed.operation_id,
            "operation_completed",
            step_id=claimed.current_step,
            detail={"steps_executed": list(outcome.steps_executed)},
        )
    elif claimed.status is OperationStatus.QUARANTINED:
        store.record_event(
            claimed.operation_id,
            "operation_quarantined",
            step_id=claimed.current_step,
            detail={"reason": outcome.quarantined_reason},
        )
    return RunReceipt(claimed, claimed=True, outcome=outcome)


def _missing_remaining(
    operation: Operation,
    plan: StepPlan,
    handlers: Mapping[Engine, StepHandler] | StepHandlerResolver,
) -> tuple[StepSpec, ...]:
    remaining = tuple(plan.step(step_id) for step_id in operation.resume_plan(plan))
    missing_method = getattr(handlers, "missing", None)
    if callable(missing_method):
        missing_ids = {spec.step_id for spec in missing_method(plan)}
        return tuple(spec for spec in remaining if spec.step_id in missing_ids)
    if not isinstance(handlers, Mapping):
        return tuple(spec for spec in remaining if handlers.resolve(spec) is None)
    return tuple(spec for spec in remaining if handlers.get(spec.engine) is None)


__all__ = [
    "RunReceipt",
    "SagaRuntimeError",
    "run_operation",
]
