"""Operation state machine, lease and checkpoints.

PostgreSQL is the workflow authority: the operation row is the single place
that answers "where did this cross-engine action stop?".  Workers claim an
operation under a lease so a crash cannot leave it RUNNING forever, and long
flows resume from the last checkpoint instead of restarting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .barrier import PublishBarrier, ResourceState
from .steps import StepPlan, StepResult
from .types import OperationStatus, StepStatus, TERMINAL_STATUSES

_VALID_TRANSITIONS: dict[OperationStatus, frozenset[OperationStatus]] = {
    OperationStatus.PENDING: frozenset(
        {OperationStatus.RUNNING, OperationStatus.FAILED, OperationStatus.QUARANTINED}
    ),
    OperationStatus.RUNNING: frozenset(
        {
            OperationStatus.COMPENSATING,
            OperationStatus.COMPLETED,
            OperationStatus.FAILED,
            OperationStatus.REQUIRES_RECONCILE,
            OperationStatus.QUARANTINED,
        }
    ),
    OperationStatus.COMPENSATING: frozenset(
        {OperationStatus.FAILED, OperationStatus.REQUIRES_RECONCILE, OperationStatus.QUARANTINED}
    ),
    OperationStatus.REQUIRES_RECONCILE: frozenset(
        {OperationStatus.RUNNING, OperationStatus.FAILED, OperationStatus.QUARANTINED}
    ),
    OperationStatus.COMPLETED: frozenset(),
    OperationStatus.FAILED: frozenset(),
    OperationStatus.QUARANTINED: frozenset(),
}


class OperationStateError(RuntimeError):
    """Raised when the operation state machine is driven out of order."""


# Y13: bound transition history — RUNNING ↔ REQUIRES_RECONCILE loops could
# otherwise grow the list (and the in-memory operation) without limit.
OPERATION_HISTORY_LIMIT = 64


@dataclass
class OperationLease:
    claimed_by: str = ""
    claimed_at: float = 0.0
    lease_until: float = 0.0
    worker_generation: int = 0

    def active(self, *, now: float) -> bool:
        return bool(self.claimed_by) and now < self.lease_until

    def claim(self, worker: str, *, now: float, lease_seconds: float, generation: int = 0) -> None:
        if not worker:
            raise OperationStateError("LEASE_WORKER_REQUIRED")
        if lease_seconds <= 0:
            raise OperationStateError("LEASE_SECONDS_INVALID")
        self.claimed_by = worker
        self.claimed_at = now
        self.lease_until = now + lease_seconds
        self.worker_generation = generation

    def expired(self, *, now: float) -> bool:
        return bool(self.claimed_by) and now >= self.lease_until

    def release(self) -> None:
        self.claimed_by = ""
        self.claimed_at = 0.0
        self.lease_until = 0.0


@dataclass
class Operation:
    operation_id: str
    operation_type: str
    module_id: str
    status: OperationStatus = OperationStatus.PENDING
    current_step: str = ""
    idempotency_key: str = ""
    correlation_id: str = ""
    fingerprint: str = ""
    generation: int = 0
    resource_id: str = ""
    lease: OperationLease = field(default_factory=OperationLease)
    steps: dict[str, StepResult] = field(default_factory=dict)
    checkpoint: dict[str, Any] = field(default_factory=dict)
    history: list[str] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    completed_at: float | None = None

    # -- state machine -----------------------------------------------------

    def transition(self, target: OperationStatus, *, now: float | None = None) -> OperationStatus:
        if target not in _VALID_TRANSITIONS[self.status]:
            raise OperationStateError(f"OPERATION_INVALID_TRANSITION:{self.status.value}->{target.value}")
        if self.status in TERMINAL_STATUSES:
            raise OperationStateError(f"OPERATION_TERMINAL:{self.status.value}")
        self.history.append(f"{self.status.value}->{target.value}")
        if len(self.history) > OPERATION_HISTORY_LIMIT:
            del self.history[: len(self.history) - OPERATION_HISTORY_LIMIT]
        self.status = target
        if now is not None:
            self.updated_at = now
        if target in TERMINAL_STATUSES and now is not None:
            self.completed_at = now
        return self.status

    # -- steps -------------------------------------------------------------

    def record_step(self, result: StepResult, *, now: float) -> None:
        self.steps[result.step_id] = result
        if result.status == StepStatus.COMPLETED.value:
            self.checkpoint["last_completed_step"] = result.step_id
        self.updated_at = now

    def last_completed_step(self) -> str:
        return str(self.checkpoint.get("last_completed_step") or "")

    def resume_plan(self, plan: StepPlan) -> tuple[str, ...]:
        """Steps still to run after the last checkpoint (checkpoint resume)."""
        done = self.last_completed_step()
        if not done:
            return tuple(step.step_id for step in plan.ordered())
        seen = False
        remaining: list[str] = []
        for step in plan.ordered():
            if seen:
                remaining.append(step.step_id)
            if step.step_id == done:
                seen = True
        if not seen:
            raise OperationStateError(f"CHECKPOINT_STEP_UNKNOWN:{done}")
        return tuple(remaining)

    def is_stale(self, *, now: float) -> bool:
        return self.status is OperationStatus.RUNNING and self.lease.expired(now=now)

    def heartbeat(self, *, now: float, lease_seconds: float) -> None:
        if self.status is not OperationStatus.RUNNING:
            return
        self.lease.lease_until = now + lease_seconds
        self.updated_at = now


__all__ = [
    "Operation",
    "OperationLease",
    "OperationStateError",
    "PublishBarrier",
    "ResourceState",
]
