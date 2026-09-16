"""Core types for the cross-engine workflow (Saga) layer.

Single-engine work stays in an ACID transaction; anything touching two or
more engines (PostgreSQL / SQLite / Qdrant / NTFS) is a multi-engine
workflow with explicit operation state, idempotent steps and bounded
recovery strategies.  No distributed transactions / 2PC.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Final


class Engine(Enum):
    POSTGRESQL = "postgresql"
    SQLITE = "sqlite"
    QDRANT = "qdrant"
    FILESYSTEM = "filesystem"
    MODEL = "model"


class OperationStatus(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPENSATING = "COMPENSATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    REQUIRES_RECONCILE = "REQUIRES_RECONCILE"
    QUARANTINED = "QUARANTINED"


class StepStatus(Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    UNKNOWN = "UNKNOWN"
    COMPENSATED = "COMPENSATED"
    INVALIDATED = "INVALIDATED"
    SUPERSEDED = "SUPERSEDED"
    SKIPPED = "SKIPPED"


class OutcomeStrategy(Enum):
    """How a step failure is handled (never fake rollback)."""

    ROLLBACK = "rollback"
    COMPENSATE = "compensate"
    INVALIDATE = "invalidate"
    SUPERSEDE = "supersede"
    RECONCILE = "reconcile"
    APPEND_ONLY = "append_only"
    RETRY_IDEMPOTENT = "retry_idempotent"
    VERIFY_THEN_DECIDE = "verify_then_decide"


SAGA_EVENTS: Final[tuple[str, ...]] = (
    "operation_created",
    "step_started",
    "step_completed",
    "step_failed",
    "compensation_started",
    "compensation_completed",
    "operation_completed",
    "operation_quarantined",
)

# Bounded retry telemetry stays local; only the saga events above are central.
LOCAL_ONLY_TELEMETRY: Final[tuple[str, ...]] = ("step_retry_attempt", "heartbeat")

TERMINAL_STATUSES: Final[frozenset[OperationStatus]] = frozenset(
    {
        OperationStatus.COMPLETED,
        OperationStatus.FAILED,
        OperationStatus.QUARANTINED,
    }
)


@dataclass(frozen=True)
class OperationFacts:
    operation_type: str
    module_id: str
    resource_id: str = ""
    revision: str = ""
    payload_hash: str = ""


__all__ = [
    "Engine",
    "LOCAL_ONLY_TELEMETRY",
    "OperationFacts",
    "OperationStatus",
    "OutcomeStrategy",
    "SAGA_EVENTS",
    "StepStatus",
    "TERMINAL_STATUSES",
]
