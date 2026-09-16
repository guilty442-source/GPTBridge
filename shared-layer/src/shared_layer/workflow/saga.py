"""Saga executor for multi-engine operations.

Contract:

  * every step is idempotent and resumable (checkpoint driven),
  * a timeout is not a failure — verify first, then decide,
  * failures follow the pre-declared compensation table, never improvised
    rollback: audit appends stay, published data is invalidated or
    superseded, cross-engine residue is reconciled,
  * retries are bounded; exhausted/unknown states end in
    REQUIRES_RECONCILE or QUARANTINED, both of which are visible,
  * long-running steps heartbeat; short SQL steps do not,
  * no cross-engine step runs inside an open PostgreSQL transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .operation import Operation
from .steps import StepPlan, StepResult, StepSpec
from .transaction_guard import assert_short_transaction
from .types import Engine, OperationStatus, OutcomeStrategy, StepStatus


class StepHandler(Protocol):
    def perform(self, operation: Operation, step: StepSpec) -> StepResult: ...

    def verify(self, operation: Operation, step: StepSpec) -> bool: ...

    def compensate(self, operation: Operation, step: StepSpec, result: StepResult) -> StepResult: ...


@dataclass
class SagaOutcome:
    operation: Operation
    steps_executed: list[str] = field(default_factory=list)
    compensations: list[str] = field(default_factory=list)
    quarantined_reason: str = ""

    @property
    def ok(self) -> bool:
        return self.operation.status is OperationStatus.COMPLETED


class SagaExecutor:
    def __init__(
        self,
        *,
        max_attempts: int = 3,
        lease_seconds: float = 120.0,
        heartbeat_seconds: float = 30.0,
    ) -> None:
        self.max_attempts = max(1, int(max_attempts))
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds

    def run(
        self,
        operation: Operation,
        plan: StepPlan,
        handlers: dict[Engine, StepHandler],
        *,
        now: float,
        pg_transaction_open: bool = False,
        sleep: Callable[[float], None] | None = None,
    ) -> SagaOutcome:
        outcome = SagaOutcome(operation=operation)
        assert_short_transaction(pg_transaction_open)
        if operation.status is not OperationStatus.RUNNING:
            operation.transition(OperationStatus.RUNNING, now=now)

        for step_id in operation.resume_plan(plan):
            spec = plan.step(step_id)
            handler = handlers.get(spec.engine)
            if handler is None:
                outcome.quarantined_reason = f"NO_HANDLER:{spec.engine.value}"
                operation.transition(OperationStatus.QUARANTINED, now=now)
                return outcome

            operation.current_step = step_id
            attempt = 0
            while True:
                attempt += 1
                result = self._perform(handler, operation, spec)
                if result.status == StepStatus.COMPLETED.value:
                    operation.record_step(result, now=now)
                    outcome.steps_executed.append(step_id)
                    break
                if result.status in (StepStatus.TIMEOUT.value, StepStatus.UNKNOWN.value):
                    result = self._resolve_uncertain(handler, operation, spec, result)
                    if result.status == StepStatus.COMPLETED.value:
                        operation.record_step(result, now=now)
                        outcome.steps_executed.append(step_id)
                        break
                if spec.long_running:
                    operation.heartbeat(now=now, lease_seconds=self.lease_seconds)
                if attempt < self.max_attempts and spec.strategy in (
                    OutcomeStrategy.RETRY_IDEMPOTENT,
                    OutcomeStrategy.VERIFY_THEN_DECIDE,
                ):
                    if sleep is not None:
                        sleep(min(2.0 ** (attempt - 1), 8.0))
                    continue
                return self._compensate(operation, spec, result, handler, outcome, now=now)

        operation.record_step(
            StepResult(
                step_id=operation.last_completed_step() or plan.ordered()[-1].step_id,
                status=StepStatus.COMPLETED.value,
                payload={"verified": True},
            ),
            now=now,
        )
        operation.transition(OperationStatus.COMPLETED, now=now)
        return outcome

    def _perform(self, handler: StepHandler, operation: Operation, spec: StepSpec) -> StepResult:
        try:
            return handler.perform(operation, spec)
        except TimeoutError as error:
            return StepResult(spec.step_id, StepStatus.TIMEOUT.value, detail=str(error))
        except Exception as error:  # noqa: BLE001 - classified below
            return StepResult(
                spec.step_id,
                StepStatus.FAILED.value,
                detail=f"{type(error).__name__}: {error}",
                error_code=type(error).__name__,
            )

    def _resolve_uncertain(
        self,
        handler: StepHandler,
        operation: Operation,
        spec: StepSpec,
        result: StepResult,
    ) -> StepResult:
        """Timeout/UNKNOWN: look the effect up before deciding."""
        try:
            if handler.verify(operation, spec):
                return StepResult(
                    spec.step_id,
                    StepStatus.COMPLETED.value,
                    detail="verified-after-uncertainty",
                    result_hash=result.result_hash,
                )
        except Exception as error:  # noqa: BLE001 - stays uncertain
            return StepResult(
                spec.step_id,
                StepStatus.UNKNOWN.value,
                detail=f"verify-failed:{type(error).__name__}",
            )
        return StepResult(spec.step_id, StepStatus.UNKNOWN.value, detail="effect-not-found")

    def _compensate(
        self,
        operation: Operation,
        spec: StepSpec,
        result: StepResult,
        handler: StepHandler,
        outcome: SagaOutcome,
        *,
        now: float,
    ) -> SagaOutcome:
        operation.record_step(result, now=now)
        operation.transition(OperationStatus.COMPENSATING, now=now)
        strategy = spec.strategy
        if strategy is OutcomeStrategy.APPEND_ONLY:
            outcome.compensations.append(f"{spec.step_id}:append-only")
            operation.transition(OperationStatus.REQUIRES_RECONCILE, now=now)
            return outcome
        if strategy is OutcomeStrategy.RECONCILE:
            outcome.compensations.append(f"{spec.step_id}:reconcile-queued")
            operation.transition(OperationStatus.REQUIRES_RECONCILE, now=now)
            return outcome
        try:
            compensated = handler.compensate(operation, spec, result)
        except Exception as error:  # noqa: BLE001 - quarantine on unhandled
            outcome.quarantined_reason = f"COMPENSATION_FAILED:{spec.step_id}:{type(error).__name__}"
            operation.transition(OperationStatus.QUARANTINED, now=now)
            return outcome
        outcome.compensations.append(f"{spec.step_id}:{strategy.value}")
        if compensated.status in (StepStatus.COMPENSATED.value, StepStatus.INVALIDATED.value, StepStatus.SUPERSEDED.value):
            operation.record_step(compensated, now=now)
        operation.transition(OperationStatus.FAILED, now=now)
        return outcome


__all__ = ["SagaExecutor", "SagaOutcome", "StepHandler"]
