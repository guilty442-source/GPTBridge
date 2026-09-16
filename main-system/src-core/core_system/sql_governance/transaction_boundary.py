"""Transaction Boundary Enforcement — A500 實作。

每個 governed operation 必須宣告 transaction boundary。
"""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

import psycopg
from psycopg.rows import dict_row

from .sql_concurrency import (
    TransactionBoundary,
    TransactionPolicy,
    ResourceClass,
    IsolationRequirement,
    RetryClassification,
    SqlFault,
    FAULT_RETRY_CLASSIFICATION,
)

_logger = logging.getLogger("gptbridge.sql.transaction")


class TransactionBoundaryError(Exception):
    """Transaction boundary violation."""
    pass


class IdempotencyCollisionError(TransactionBoundaryError):
    """Idempotency key collision."""
    pass


class ConcurrentModificationError(TransactionBoundaryError):
    """Optimistic lock failure - concurrent modification."""
    pass


class StaleRevisionError(TransactionBoundaryError):
    """Revision mismatch - stale read."""
    pass


class TransactionExecutor:
    """Enforces transaction boundaries per A500."""

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn
        self._conn: Optional[psycopg.Connection] = None
        self._current_boundary: Optional[TransactionBoundary] = None

    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(
                self.dsn,
                row_factory=dict_row,
                autocommit=False,
            )
        return self._conn

    def close(self) -> None:
        if self._conn and not self._conn.closed:
            self._conn.close()
        self._conn = None

    @contextmanager
    def execute(self, boundary: TransactionBoundary):
        """Execute operation within declared transaction boundary.

        Args:
            boundary: Declared transaction boundary (A500)

        Yields:
            Cursor for executing operations

        Raises:
            TransactionBoundaryError: On boundary violation
        """
        self._current_boundary = boundary
        conn = self._get_conn()

        # Set isolation level based on resource class (A501)
        isolation = self._get_isolation_level(boundary.resource_class)
        conn.execute(f"SET TRANSACTION ISOLATION LEVEL {isolation.value}")

        # Set session variables for audit (A505)
        self._set_session_context(conn, boundary)

        try:
            with conn.cursor() as cur:
                yield cur

            # Commit condition check (A500)
            if not self._check_commit_condition(boundary):
                raise TransactionBoundaryError(f"Commit condition not met: {boundary.commit_condition}")

            conn.commit()
            _logger.info(
                "TransactionExecutor: committed operation=%s policy=%s",
                boundary.operation_id, boundary.transaction_policy.value
            )

        except psycopg.errors.SerializationFailure as e:
            conn.rollback()
            _logger.warning("TransactionExecutor: serialization failure op=%s", boundary.operation_id)
            raise self._map_fault(SqlFault.SERIALIZATION_FAILURE, e)

        except psycopg.errors.DeadlockDetected as e:
            conn.rollback()
            _logger.warning("TransactionExecutor: deadlock op=%s", boundary.operation_id)
            raise self._map_fault(SqlFault.DEADLOCK, e)

        except psycopg.errors.LockNotAvailable as e:
            conn.rollback()
            _logger.warning("TransactionExecutor: lock timeout op=%s", boundary.operation_id)
            raise self._map_fault(SqlFault.LOCK_TIMEOUT, e)

        except psycopg.errors.UniqueViolation as e:
            conn.rollback()
            if "idempotency" in str(e).lower():
                raise self._map_fault(SqlFault.IDEMPOTENCY_COLLISION, e)
            raise self._map_fault(SqlFault.DUPLICATE_OPERATION_EFFECT, e)

        except Exception as e:
            conn.rollback()
            if not self._check_rollback_condition(boundary, e):
                _logger.error("TransactionExecutor: unexpected error, rollback forced: %s", e)
            raise

        finally:
            self._current_boundary = None

    def _get_isolation_level(self, resource_class: ResourceClass) -> IsolationRequirement:
        """A501: Map resource class to isolation requirement."""
        from .sql_concurrency import RESOURCE_CLASS_ISOLATION
        return RESOURCE_CLASS_ISOLATION.get(resource_class, IsolationRequirement.READ_COMMITTED)

    def _set_session_context(self, conn: psycopg.Connection, boundary: TransactionBoundary) -> None:
        """A505: Set session context variables."""
        # These would be set by the caller based on SessionBinding
        pass

    def _check_commit_condition(self, boundary: TransactionBoundary) -> bool:
        """Verify commit condition (A500)."""
        # In practice, check that all expected side effects occurred
        # For now, always true if no exception
        return True

    def _check_rollback_condition(self, boundary: TransactionBoundary, error: Exception) -> bool:
        """Verify rollback condition (A500)."""
        # Default: any error triggers rollback
        return True

    def _map_fault(self, fault: SqlFault, original: Exception) -> Exception:
        """Map PostgreSQL error to formal SQL fault."""
        retry_class = FAULT_RETRY_CLASSIFICATION.get(fault, RetryClassification.NON_RETRYABLE)

        if retry_class == RetryClassification.REQUIRES_REEVALUATION:
            return ConcurrentModificationError(
                f"{fault.value}: {original}. Must re-read and re-adjudicate."
            )
        elif retry_class == RetryClassification.SAFE_RETRY:
            return TransactionBoundaryError(
                f"{fault.value}: {original}. Safe to retry."
            )
        else:
            return TransactionBoundaryError(
                f"{fault.value}: {original}. Non-retryable."
            )


def declare_boundary(
    operation_id: Optional[str] = None,
    policy: TransactionPolicy = TransactionPolicy.ATOMIC_SINGLE_DATABASE,
    resource_class: ResourceClass = ResourceClass.CENTRAL_OFFICIAL_DATA,
    scope: tuple[str, ...] = (),
    side_effects: tuple[str, ...] = (),
    commit_cond: str = "all_writes_succeeded",
    rollback_cond: str = "any_write_failed",
    concurrency: str = "optimistic_lock",
    locking: str = "row_level",
    conflict: str = "fail_on_conflict",
    idempotency_key: Optional[str] = None,
    operation_class: Optional[str] = None,
) -> TransactionBoundary:
    """Helper to declare transaction boundary (A500)."""
    return TransactionBoundary(
        operation_id=operation_id or f"txn-{uuid.uuid4().hex[:12]}",
        transaction_policy=policy,
        transaction_scope=scope,
        expected_side_effects=side_effects,
        commit_condition=commit_cond,
        rollback_condition=rollback_cond,
        resource_class=resource_class,
        isolation_requirement=IsolationRequirement.READ_COMMITTED,  # Filled by executor
        concurrency_policy=concurrency,
        locking_policy=locking,
        conflict_policy=conflict,
        idempotency_key=idempotency_key,
        operation_class=operation_class,
    )


__all__ = [
    "TransactionExecutor",
    "declare_boundary",
    "TransactionBoundaryError",
    "IdempotencyCollisionError",
    "ConcurrentModificationError",
    "StaleRevisionError",
]