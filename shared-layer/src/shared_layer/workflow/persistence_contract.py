"""Saga store contract: error type, event whitelist, store protocol (migration 113)."""
from __future__ import annotations

from typing import Any, Mapping, Protocol

from .operation import Operation
from .steps import StepResult, StepSpec
from .types import SAGA_EVENTS


class SagaStoreError(RuntimeError):
    """Raised when the store contract cannot be honoured (fail-closed)."""



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

    def list_operations(self, limit: int = 50) -> list[dict[str, Any]]: ...

    def list_step_rows(self, operation_id: str) -> list[dict[str, Any]]: ...

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
