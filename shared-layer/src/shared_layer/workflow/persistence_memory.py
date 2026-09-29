"""In-memory saga store: same claim/lease semantics, offline mirror / tests."""
from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping

from .operation import Operation
from .persistence_contract import SagaStoreError, validate_event_type
from .steps import StepResult, StepSpec
from .types import OperationStatus


# ---------------------------------------------------------------------------
# In-memory implementation (offline mirror / tests)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredEvent:
    operation_id: str
    event_type: str
    step_id: str
    detail: dict[str, Any]
    recorded_at: float


@dataclass
class InMemorySagaStore:
    """Same claim/lease semantics as the SQL contract, in process memory."""

    clock: Callable[[], float] = time.time
    _operations: dict[str, Operation] = field(default_factory=dict)
    _steps: dict[str, dict[str, tuple[StepSpec, StepResult, int]]] = field(default_factory=dict)
    _events: list[StoredEvent] = field(default_factory=list)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Interface parity with ``PostgresSagaStore.transaction`` — the
        in-memory store has no commit boundary, so this is a no-op."""
        yield

    # -- operations ---------------------------------------------------------

    def create_operation(self, operation: Operation) -> tuple[Operation, bool]:
        if not operation.operation_id:
            raise SagaStoreError("OPERATION_ID_REQUIRED")
        existing = self._find_existing(operation)
        if existing is not None:
            return copy.deepcopy(existing), False
        stored = copy.deepcopy(operation)
        if stored.created_at <= 0:
            stored.created_at = self.clock()
        self._operations[stored.operation_id] = stored
        self._steps.setdefault(stored.operation_id, {})
        self.record_event(
            stored.operation_id,
            "operation_created",
            detail={"operation_type": stored.operation_type, "module_id": stored.module_id},
        )
        return copy.deepcopy(stored), True

    def load_operation(self, operation_id: str) -> Operation | None:
        stored = self._operations.get(operation_id)
        return copy.deepcopy(stored) if stored is not None else None

    def list_operations(self, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        ordered = sorted(
            self._operations.values(),
            key=lambda op: (op.created_at, op.operation_id),
            reverse=True,
        )
        return [
            {
                "operation_id": op.operation_id,
                "operation_type": op.operation_type,
                "module_id": op.module_id,
                "resource_id": op.resource_id,
                "generation": int(op.generation),
                "status": op.status.value,
                "current_step": op.current_step,
                "correlation_id": op.correlation_id,
                "created_at": op.created_at,
                "updated_at": op.updated_at,
                "completed_at": op.completed_at,
            }
            for op in ordered[:bounded]
        ]

    def list_step_rows(self, operation_id: str) -> list[dict[str, Any]]:
        rows = []
        for spec, result, attempt in self._steps.get(operation_id, {}).values():
            rows.append({
                "step_id": result.step_id or spec.step_id,
                "step_order": int(spec.step_order),
                "engine": spec.engine.value,
                "action_type": spec.action_type,
                "status": result.status,
                "attempt_count": max(1, int(attempt)),
                "error_code": result.error_code,
            })
        rows.sort(key=lambda row: (row["step_order"], row["step_id"]))
        return rows

    def claim_operation(
        self, operation_id: str, *, worker: str, lease_seconds: float
    ) -> Operation | None:
        self._require_worker(worker)
        stored = self._operations.get(operation_id)
        if stored is None or not self._claimable(stored):
            return None
        self._take_lease(stored, worker, lease_seconds)
        return copy.deepcopy(stored)

    def claim_next_operation(
        self, *, worker: str, lease_seconds: float
    ) -> Operation | None:
        self._require_worker(worker)
        candidates = sorted(self._operations.values(), key=lambda op: (op.created_at, op.operation_id))
        for stored in candidates:
            if self._claimable(stored):
                self._take_lease(stored, worker, lease_seconds)
                return copy.deepcopy(stored)
        return None

    def claim_next_reconcile(
        self, *, worker: str, lease_seconds: float
    ) -> Operation | None:
        self._require_worker(worker)
        candidates = sorted(
            (
                op
                for op in self._operations.values()
                if op.status is OperationStatus.REQUIRES_RECONCILE
            ),
            key=lambda op: (op.updated_at, op.created_at, op.operation_id),
        )
        for stored in candidates:
            if not stored.lease.active(now=self.clock()):
                now = self.clock()
                stored.lease.claim(worker, now=now, lease_seconds=lease_seconds)
                stored.updated_at = now
                return copy.deepcopy(stored)
        return None

    def heartbeat(self, operation_id: str, *, worker: str, lease_seconds: float) -> bool:
        self._require_worker(worker)
        stored = self._operations.get(operation_id)
        if stored is None or stored.status is not OperationStatus.RUNNING:
            return False
        if stored.lease.claimed_by != worker:
            return False
        stored.heartbeat(now=self.clock(), lease_seconds=lease_seconds)
        stored.lease.claimed_by = worker
        return True

    def save_operation(self, operation: Operation) -> None:
        self._operations[operation.operation_id] = copy.deepcopy(operation)

    def save_step(
        self, operation_id: str, spec: StepSpec, result: StepResult, *, attempt: int
    ) -> None:
        self._steps.setdefault(operation_id, {})[result.step_id or spec.step_id] = (
            spec,
            copy.deepcopy(result),
            max(1, int(attempt)),
        )

    def record_event(
        self,
        operation_id: str,
        event_type: str,
        *,
        step_id: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        validate_event_type(event_type)
        self._events.append(
            StoredEvent(operation_id, event_type, step_id, dict(detail or {}), self.clock())
        )

    # -- read helpers (non-canonical, for tests / diagnostics) --------------

    def events(self, operation_id: str | None = None) -> tuple[StoredEvent, ...]:
        if operation_id is None:
            return tuple(self._events)
        return tuple(event for event in self._events if event.operation_id == operation_id)

    def stored_steps(self, operation_id: str) -> dict[str, tuple[StepSpec, StepResult, int]]:
        return dict(self._steps.get(operation_id, {}))

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _require_worker(worker: str) -> None:
        if not worker:
            raise SagaStoreError("WORKER_ID_REQUIRED")

    def _claimable(self, operation: Operation) -> bool:
        if operation.status not in (
            OperationStatus.PENDING,
            OperationStatus.RUNNING,
            OperationStatus.REQUIRES_RECONCILE,
        ):
            return False
        return not operation.lease.active(now=self.clock())

    def _take_lease(self, stored: Operation, worker: str, lease_seconds: float) -> None:
        now = self.clock()
        if stored.status is not OperationStatus.RUNNING:
            stored.transition(OperationStatus.RUNNING, now=now)
        stored.lease.claim(worker, now=now, lease_seconds=lease_seconds)
        stored.updated_at = now

    def _find_existing(self, operation: Operation) -> Operation | None:
        existing = self._operations.get(operation.operation_id)
        if existing is not None:
            return existing
        if operation.idempotency_key:
            for stored in self._operations.values():
                if stored.idempotency_key == operation.idempotency_key:
                    return stored
        if operation.fingerprint:
            for stored in self._operations.values():
                if (
                    stored.module_id == operation.module_id
                    and stored.operation_type == operation.operation_type
                    and stored.fingerprint == operation.fingerprint
                ):
                    return stored
        return None
