"""Durable operation authority on the migration-113 SQL contract.

``gptbridge_workflow.operation`` / ``operation_step`` / ``operation_event``
are the single authority for multi-engine work.  This module provides the
store contract plus two implementations:

  * ``PostgresSagaStore`` — parameterized SQL against migration
    ``113_workflow_operation.sql`` (lease ``claimed_by`` / ``lease_until``,
    idempotency key, checkpoints, events).  No new tables and no new columns.
  * ``InMemorySagaStore`` — an offline mirror with the same claim/lease
    semantics, used before PostgreSQL is reachable and in tests.  It is
    explicitly non-canonical: it can never declare central completion on its
    own, exactly like the SQLite fallback.

Nothing here decides business outcomes; the store only persists and claims
what the saga / reconcile workers decide.
"""

from __future__ import annotations

import copy
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, ContextManager, Iterator, Mapping, Protocol

from .operation import Operation, OperationLease
from .steps import StepResult, StepSpec
from .types import SAGA_EVENTS, OperationStatus


class SagaStoreError(RuntimeError):
    """Raised when the store contract cannot be honoured (fail-closed)."""


# ---------------------------------------------------------------------------
# SQL contract (migration 113) — every statement is fully parameterized.
# ---------------------------------------------------------------------------

OPERATION_INSERT_SQL = (
    "INSERT INTO gptbridge_workflow.operation "
    "(operation_id, operation_type, module_id, resource_id, generation, status, "
    "current_step, idempotency_key, correlation_id, fingerprint, checkpoint, "
    "created_at, updated_at) "
    "VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, %s, %s, %s, %s, now(), now()) "
    "ON CONFLICT DO NOTHING RETURNING operation_id"
)

OPERATION_LOAD_SQL = (
    "SELECT operation_id, operation_type, module_id, resource_id, generation, status, "
    "current_step, idempotency_key, correlation_id, fingerprint, claimed_by, "
    "claimed_at, lease_until, worker_generation, checkpoint, created_at, updated_at, "
    "completed_at FROM gptbridge_workflow.operation WHERE operation_id = %s"
)

OPERATION_LOAD_BY_KEY_SQL = (
    "SELECT operation_id FROM gptbridge_workflow.operation WHERE idempotency_key = %s "
    "ORDER BY created_at LIMIT 1"
)

OPERATION_LOAD_BY_FINGERPRINT_SQL = (
    "SELECT operation_id FROM gptbridge_workflow.operation "
    "WHERE module_id = %s AND operation_type = %s AND fingerprint = %s "
    "ORDER BY created_at LIMIT 1"
)

OPERATION_CLAIM_SQL = (
    "UPDATE gptbridge_workflow.operation "
    "SET status = 'RUNNING', claimed_by = %s, claimed_at = now(), "
    "lease_until = now() + make_interval(secs => %s), updated_at = now() "
    "WHERE operation_id = %s "
    "AND status IN ('PENDING', 'RUNNING', 'REQUIRES_RECONCILE') "
    "AND (lease_until IS NULL OR lease_until < now()) "
    "RETURNING operation_id"
)

# Existing migration function: claims PENDING / RUNNING / REQUIRES_RECONCILE.
OPERATION_CLAIM_NEXT_SQL = (
    "SELECT gptbridge_workflow.claim_next_operation(%s, %s) AS operation_id"
)

# Reconcile claim: same lease discipline, but the operation stays parked in
# REQUIRES_RECONCILE until the callback's verdict moves it.
RECONCILE_CLAIM_SQL = (
    "WITH candidate AS ("
    "SELECT operation_id FROM gptbridge_workflow.operation "
    "WHERE status = 'REQUIRES_RECONCILE' "
    "AND (lease_until IS NULL OR lease_until < now()) "
    "ORDER BY updated_at, created_at LIMIT 1 FOR UPDATE SKIP LOCKED"
    ") "
    "UPDATE gptbridge_workflow.operation target "
    "SET claimed_by = %s, claimed_at = now(), "
    "lease_until = now() + make_interval(secs => %s), updated_at = now() "
    "FROM candidate WHERE target.operation_id = candidate.operation_id "
    "RETURNING target.operation_id"
)

OPERATION_HEARTBEAT_SQL = (
    "UPDATE gptbridge_workflow.operation "
    "SET lease_until = now() + make_interval(secs => %s), updated_at = now() "
    "WHERE operation_id = %s AND claimed_by = %s AND status = 'RUNNING' "
    "RETURNING operation_id"
)

OPERATION_SAVE_SQL = (
    "UPDATE gptbridge_workflow.operation "
    "SET status = %s, current_step = %s, checkpoint = %s, worker_generation = %s, "
    "claimed_by = %s, claimed_at = to_timestamp(NULLIF(%s, 0)), "
    "lease_until = to_timestamp(NULLIF(%s, 0)), updated_at = now(), "
    "completed_at = to_timestamp(NULLIF(%s, 0)) "
    "WHERE operation_id = %s"
)

OPERATION_STEP_UPSERT_SQL = (
    "INSERT INTO gptbridge_workflow.operation_step "
    "(operation_id, step_id, step_order, engine, action_type, status, "
    "attempt_count, started_at, completed_at, result_hash, error_code) "
    "VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now(), %s, %s) "
    "ON CONFLICT (operation_id, step_id) DO UPDATE SET "
    "status = excluded.status, attempt_count = excluded.attempt_count, "
    "completed_at = excluded.completed_at, result_hash = excluded.result_hash, "
    "error_code = excluded.error_code"
)

OPERATION_STEP_LIST_SQL = (
    "SELECT step_id, step_order, engine, action_type, status, attempt_count, "
    "result_hash, error_code FROM gptbridge_workflow.operation_step "
    "WHERE operation_id = %s ORDER BY step_order, step_id"
)

OPERATION_EVENT_INSERT_SQL = (
    "INSERT INTO gptbridge_workflow.operation_event "
    "(operation_id, event_type, step_id, detail, created_at) "
    "VALUES (%s, %s, %s, %s, now())"
)


def validate_event_type(event_type: str) -> str:
    """Only the declared SAGA_EVENTS may reach ``operation_event``."""
    if event_type not in SAGA_EVENTS:
        raise ValueError(f"UNKNOWN_SAGA_EVENT:{event_type}")
    return event_type


# ---------------------------------------------------------------------------
# Store contract
# ---------------------------------------------------------------------------


class SagaStore(Protocol):
    def create_operation(self, operation: Operation) -> tuple[Operation, bool]: ...

    def load_operation(self, operation_id: str) -> Operation | None: ...

    def claim_operation(
        self, operation_id: str, *, worker: str, lease_seconds: float
    ) -> Operation | None: ...

    def claim_next_operation(
        self, *, worker: str, lease_seconds: float
    ) -> Operation | None: ...

    def claim_next_reconcile(
        self, *, worker: str, lease_seconds: float
    ) -> Operation | None: ...

    def heartbeat(self, operation_id: str, *, worker: str, lease_seconds: float) -> bool: ...

    def save_operation(self, operation: Operation) -> None: ...

    def save_step(
        self, operation_id: str, spec: StepSpec, result: StepResult, *, attempt: int
    ) -> None: ...

    def record_event(
        self,
        operation_id: str,
        event_type: str,
        *,
        step_id: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None: ...


# ---------------------------------------------------------------------------
# PostgreSQL implementation
# ---------------------------------------------------------------------------


def _epoch(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return float(value.timestamp())


def _operation_from_row(row: Mapping[str, Any], step_rows: list[Mapping[str, Any]]) -> Operation:
    lease = OperationLease(
        claimed_by=str(row["claimed_by"] or ""),
        claimed_at=_epoch(row["claimed_at"]),
        lease_until=_epoch(row["lease_until"]),
        worker_generation=int(row["worker_generation"] or 0),
    )
    steps: dict[str, StepResult] = {}
    for step_row in step_rows or ():
        step_id = str(step_row["step_id"])
        steps[step_id] = StepResult(
            step_id=step_id,
            status=str(step_row["status"]),
            result_hash=str(step_row["result_hash"] or ""),
            error_code=str(step_row["error_code"] or ""),
        )
    return Operation(
        operation_id=str(row["operation_id"]),
        operation_type=str(row["operation_type"]),
        module_id=str(row["module_id"]),
        status=OperationStatus(row["status"]),
        current_step=str(row["current_step"] or ""),
        idempotency_key=str(row["idempotency_key"] or ""),
        correlation_id=str(row["correlation_id"] or ""),
        fingerprint=str(row["fingerprint"] or ""),
        generation=int(row["generation"] or 0),
        resource_id=str(row["resource_id"] or ""),
        lease=lease,
        steps=steps,
        checkpoint=dict(row["checkpoint"] or {}),
        created_at=_epoch(row["created_at"]),
        updated_at=_epoch(row["updated_at"]),
        completed_at=_epoch(row["completed_at"]) or None,
    )


class PostgresSagaStore:
    """Migration-113 backed store.

    ``connection_provider`` is a callable returning a context manager that
    yields a live connection (for example ``pool.acquire`` or
    ``ConnectionManager.connection``).  Rows must be mappings (``dict_row``).
    """

    def __init__(self, connection_provider: Callable[[], ContextManager[Any]]) -> None:
        self._connection_provider = connection_provider

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        with self._connection_provider() as connection:
            yield connection

    # -- operations ---------------------------------------------------------

    def create_operation(self, operation: Operation) -> tuple[Operation, bool]:
        if not operation.operation_id:
            raise SagaStoreError("OPERATION_ID_REQUIRED")
        with self._connection() as connection:
            row = connection.execute(
                OPERATION_INSERT_SQL,
                (
                    operation.operation_id,
                    operation.operation_type,
                    operation.module_id,
                    operation.resource_id,
                    int(operation.generation),
                    operation.current_step,
                    operation.idempotency_key,
                    operation.correlation_id,
                    operation.fingerprint,
                    dict(operation.checkpoint),
                ),
            ).fetchone()
            if row is not None:
                self._insert_event(
                    connection,
                    operation.operation_id,
                    "operation_created",
                    "",
                    {
                        "operation_type": operation.operation_type,
                        "module_id": operation.module_id,
                    },
                )
                connection.commit()
                return operation, True
            existing = self._load_existing(connection, operation)
            connection.commit()
            if existing is None:
                raise SagaStoreError("OPERATION_CONFLICT_UNRESOLVED")
            return existing, False

    def load_operation(self, operation_id: str) -> Operation | None:
        with self._connection() as connection:
            operation = self._load_operation(connection, operation_id)
            connection.commit()
            return operation

    def claim_operation(
        self, operation_id: str, *, worker: str, lease_seconds: float
    ) -> Operation | None:
        self._require_worker(worker)
        with self._connection() as connection:
            row = connection.execute(
                OPERATION_CLAIM_SQL, (worker, float(lease_seconds), operation_id)
            ).fetchone()
            claimed = None
            if row is not None:
                claimed = self._load_operation(connection, operation_id)
            connection.commit()
            return claimed

    def claim_next_operation(
        self, *, worker: str, lease_seconds: float
    ) -> Operation | None:
        self._require_worker(worker)
        with self._connection() as connection:
            row = connection.execute(
                OPERATION_CLAIM_NEXT_SQL, (worker, max(1, int(lease_seconds)))
            ).fetchone()
            operation_id = str(row["operation_id"]) if row and row["operation_id"] else ""
            claimed = self._load_operation(connection, operation_id) if operation_id else None
            connection.commit()
            return claimed

    def claim_next_reconcile(
        self, *, worker: str, lease_seconds: float
    ) -> Operation | None:
        self._require_worker(worker)
        with self._connection() as connection:
            row = connection.execute(
                RECONCILE_CLAIM_SQL, (worker, float(lease_seconds))
            ).fetchone()
            claimed = None
            if row is not None:
                claimed = self._load_operation(connection, str(row["operation_id"]))
            connection.commit()
            return claimed

    def heartbeat(self, operation_id: str, *, worker: str, lease_seconds: float) -> bool:
        self._require_worker(worker)
        with self._connection() as connection:
            row = connection.execute(
                OPERATION_HEARTBEAT_SQL, (float(lease_seconds), operation_id, worker)
            ).fetchone()
            connection.commit()
            return row is not None

    def save_operation(self, operation: Operation) -> None:
        lease = operation.lease
        with self._connection() as connection:
            connection.execute(
                OPERATION_SAVE_SQL,
                (
                    operation.status.value,
                    operation.current_step,
                    dict(operation.checkpoint),
                    int(lease.worker_generation),
                    lease.claimed_by,
                    float(lease.claimed_at),
                    float(lease.lease_until),
                    float(operation.completed_at or 0.0),
                    operation.operation_id,
                ),
            )
            connection.commit()

    def save_step(
        self, operation_id: str, spec: StepSpec, result: StepResult, *, attempt: int
    ) -> None:
        with self._connection() as connection:
            connection.execute(
                OPERATION_STEP_UPSERT_SQL,
                (
                    operation_id,
                    result.step_id or spec.step_id,
                    int(spec.step_order),
                    spec.engine.value,
                    spec.action_type,
                    result.status,
                    max(1, int(attempt)),
                    result.result_hash,
                    result.error_code,
                ),
            )
            connection.commit()

    def record_event(
        self,
        operation_id: str,
        event_type: str,
        *,
        step_id: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        validate_event_type(event_type)
        with self._connection() as connection:
            self._insert_event(connection, operation_id, event_type, step_id, detail)
            connection.commit()

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _require_worker(worker: str) -> None:
        if not worker:
            raise SagaStoreError("WORKER_ID_REQUIRED")

    @staticmethod
    def _insert_event(
        connection: Any,
        operation_id: str,
        event_type: str,
        step_id: str,
        detail: Mapping[str, Any] | None,
    ) -> None:
        validate_event_type(event_type)
        connection.execute(
            OPERATION_EVENT_INSERT_SQL,
            (operation_id, event_type, step_id, dict(detail or {})),
        )

    def _load_operation(self, connection: Any, operation_id: str) -> Operation | None:
        row = connection.execute(OPERATION_LOAD_SQL, (operation_id,)).fetchone()
        if row is None:
            return None
        step_rows = connection.execute(OPERATION_STEP_LIST_SQL, (operation_id,)).fetchall()
        return _operation_from_row(row, list(step_rows or ()))

    def _load_existing(self, connection: Any, operation: Operation) -> Operation | None:
        existing = self._load_operation(connection, operation.operation_id)
        if existing is not None:
            return existing
        if operation.idempotency_key:
            row = connection.execute(
                OPERATION_LOAD_BY_KEY_SQL, (operation.idempotency_key,)
            ).fetchone()
            if row is not None:
                return self._load_operation(connection, str(row["operation_id"]))
        if operation.fingerprint:
            row = connection.execute(
                OPERATION_LOAD_BY_FINGERPRINT_SQL,
                (operation.module_id, operation.operation_type, operation.fingerprint),
            ).fetchone()
            if row is not None:
                return self._load_operation(connection, str(row["operation_id"]))
        return None


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


__all__ = [
    "InMemorySagaStore",
    "OPERATION_CLAIM_NEXT_SQL",
    "OPERATION_CLAIM_SQL",
    "OPERATION_EVENT_INSERT_SQL",
    "OPERATION_HEARTBEAT_SQL",
    "OPERATION_INSERT_SQL",
    "OPERATION_LOAD_BY_FINGERPRINT_SQL",
    "OPERATION_LOAD_BY_KEY_SQL",
    "OPERATION_LOAD_SQL",
    "OPERATION_SAVE_SQL",
    "OPERATION_STEP_LIST_SQL",
    "OPERATION_STEP_UPSERT_SQL",
    "PostgresSagaStore",
    "RECONCILE_CLAIM_SQL",
    "SagaStore",
    "SagaStoreError",
    "StoredEvent",
    "validate_event_type",
]
