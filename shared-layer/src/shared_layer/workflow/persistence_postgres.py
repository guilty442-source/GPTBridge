"""PostgreSQL saga store implementation (migration-113 SQL contract)."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, ContextManager, Iterator, Mapping

from .operation import Operation, OperationLease
from .persistence_contract import SagaStoreError, validate_event_type
from .persistence_sql import (
    OPERATION_CLAIM_NEXT_SQL,
    OPERATION_CLAIM_SQL,
    OPERATION_EVENT_INSERT_SQL,
    OPERATION_HEARTBEAT_SQL,
    OPERATION_INSERT_SQL,
    OPERATION_LIST_SQL,
    OPERATION_LOAD_BY_FINGERPRINT_SQL,
    OPERATION_LOAD_BY_KEY_SQL,
    OPERATION_LOAD_SQL,
    OPERATION_SAVE_SQL,
    OPERATION_STEP_LIST_SQL,
    OPERATION_STEP_UPSERT_SQL,
    RECONCILE_CLAIM_SQL,
)
from .steps import StepResult, StepSpec
from .types import OperationStatus


# ---------------------------------------------------------------------------
# PostgreSQL implementation
# ---------------------------------------------------------------------------


def _epoch(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    return float(value.timestamp())


def _operation_summary_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "operation_id": str(row["operation_id"]),
        "operation_type": str(row["operation_type"]),
        "module_id": str(row["module_id"]),
        "resource_id": str(row["resource_id"] or ""),
        "generation": int(row["generation"] or 0),
        "status": str(row["status"]),
        "current_step": str(row["current_step"] or ""),
        "correlation_id": str(row["correlation_id"] or ""),
        "created_at": _epoch(row["created_at"]),
        "updated_at": _epoch(row["updated_at"]),
        "completed_at": _epoch(row["completed_at"]) or None,
    }


def _step_row_projection(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "step_id": str(row["step_id"]),
        "step_order": int(row["step_order"] or 0),
        "engine": str(row["engine"] or ""),
        "action_type": str(row["action_type"] or ""),
        "status": str(row["status"] or ""),
        "attempt_count": int(row["attempt_count"] or 0),
        "error_code": str(row["error_code"] or ""),
    }


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


def _jsonb(connection: Any, value: Any) -> Any:
    """Adapt a mapping to jsonb for real psycopg connections only.

    Test doubles and legacy callers keep plain mappings so parameter
    assertions and non-PostgreSQL stores remain unchanged."""
    try:
        from psycopg import Connection
        from psycopg.types.json import Jsonb

        if isinstance(connection, Connection):
            return Jsonb(value)
    except Exception:
        pass
    return value


class PostgresSagaStore:
    """Migration-113 backed store.

    ``connection_provider`` is a callable returning a context manager that
    yields a live connection (for example ``pool.acquire`` or
    ``ConnectionManager.connection``).  Rows must be mappings (``dict_row``).
    """

    def __init__(self, connection_provider: Callable[[], ContextManager[Any]]) -> None:
        self._connection_provider = connection_provider
        self._local = threading.local()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Y6: batch subsequent store calls into one commit boundary.

        While active, every store method on this thread reuses the ambient
        connection and defers its commit; a single ``commit()`` runs when
        the block exits cleanly.  Per-step persistence in
        ``runtime.run_operation`` goes from up to 4 commits to 1.
        """
        with self._connection_provider() as connection:
            previous = getattr(self._local, "ambient", None)
            self._local.ambient = connection
            try:
                yield
                connection.commit()
            finally:
                self._local.ambient = previous

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        ambient = getattr(self._local, "ambient", None)
        if ambient is not None:
            yield ambient
            return
        with self._connection_provider() as connection:
            yield connection

    def _commit(self, connection: Any) -> None:
        """Commit unless an ambient ``transaction()`` owns the boundary."""
        if getattr(self._local, "ambient", None) is not connection:
            connection.commit()

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
                    _jsonb(connection, dict(operation.checkpoint)),
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
                self._commit(connection)
                return operation, True
            existing = self._load_existing(connection, operation)
            self._commit(connection)
            if existing is None:
                raise SagaStoreError("OPERATION_CONFLICT_UNRESOLVED")
            return existing, False

    def load_operation(self, operation_id: str) -> Operation | None:
        with self._connection() as connection:
            operation = self._load_operation(connection, operation_id)
            self._commit(connection)
            return operation

    def list_operations(self, limit: int = 50) -> list[dict[str, Any]]:
        bounded = max(1, min(int(limit), 200))
        with self._connection() as connection:
            rows = connection.execute(OPERATION_LIST_SQL, (bounded,)).fetchall()
            self._commit(connection)
            return [_operation_summary_from_row(row) for row in rows or ()]

    def list_step_rows(self, operation_id: str) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(OPERATION_STEP_LIST_SQL, (operation_id,)).fetchall()
            self._commit(connection)
            return [_step_row_projection(row) for row in rows or ()]

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
            self._commit(connection)
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
            self._commit(connection)
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
            self._commit(connection)
            return claimed

    def heartbeat(self, operation_id: str, *, worker: str, lease_seconds: float) -> bool:
        self._require_worker(worker)
        with self._connection() as connection:
            row = connection.execute(
                OPERATION_HEARTBEAT_SQL, (float(lease_seconds), operation_id, worker)
            ).fetchone()
            self._commit(connection)
            return row is not None

    def save_operation(self, operation: Operation) -> None:
        lease = operation.lease
        with self._connection() as connection:
            connection.execute(
                OPERATION_SAVE_SQL,
                (
                    operation.status.value,
                    operation.current_step,
                    _jsonb(connection, dict(operation.checkpoint)),
                    int(lease.worker_generation),
                    lease.claimed_by,
                    float(lease.claimed_at),
                    float(lease.lease_until),
                    float(operation.completed_at or 0.0),
                    operation.operation_id,
                ),
            )
            self._commit(connection)

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
            self._commit(connection)

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
            self._commit(connection)

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
            (operation_id, event_type, step_id, _jsonb(connection, dict(detail or {}))),
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
