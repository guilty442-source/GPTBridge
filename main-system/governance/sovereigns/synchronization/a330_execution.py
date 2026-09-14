"""Synchronization Sovereign — A330 Certified Update Execution (A330).

The ONLY execution power for this sovereign. Executes certified backend
updates after certification proof adjudication passes.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome, verified_basis


# A330 terminal statuses
_A330_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "global-success",
    "failed-isolated",
    "rolled-back",
    "partial-deferred",
})


class SyncA330ExecutionMixin:
    """A330 certified update execution — the sole execution exception."""

    _sub_sovereigns: dict[str, Any]
    app: Any
    workspace_root: Path
    _certified_update_operations: dict[str, dict[str, Any]]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._certified_update_operations = {}

    async def _adjudicate_a330_certified_update(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A330: execute certified update after certification proof passes."""
        update_type = request.payload.get("update_type")
        if not update_type:
            return refusal_outcome("MISSING_UPDATE_TYPE", verified_basis(("A330",)))

        if request.payload.get("certified") is not True:
            return refusal_outcome("CERTIFICATION_MISSING", verified_basis(("A330",)))

        update_set = request.payload.get("update_set")
        if not isinstance(update_set, (list, tuple)) or not update_set:
            return refusal_outcome("EMPTY_UPDATE_SET", verified_basis(("A330",)))

        artifact_hashes = request.payload.get("artifact_hashes")
        if not isinstance(artifact_hashes, dict) or not artifact_hashes:
            return refusal_outcome("MISSING_ARTIFACT_HASHES", verified_basis(("A330",)))

        operation_id = str(request.payload.get("operation_id") or "")
        if not operation_id:
            return refusal_outcome("MISSING_OPERATION_ID", verified_basis(("A330",)))

        # Idempotent replay guard
        if operation_id in self._certified_update_operations:
            existing = self._certified_update_operations[operation_id]
            existing_status = existing.get("terminal_status", "")
            if existing_status in _A330_TERMINAL_STATUSES:
                return accepted_outcome(
                    {
                        "repair_decision": "authorized",
                        "update_type": update_type,
                        "operation_id": operation_id,
                        "terminal_status": existing_status,
                        "idempotent_replay": True,
                    },
                    verified_basis(("A330",)),
                )
            return refusal_outcome("OPERATION_IN_FLIGHT", verified_basis(("A330",)))

        # Execute the certified update via the hot-update service
        hot_update = getattr(self.app, "hot_update_service", None)
        if hot_update is None:
            return refusal_outcome("HOT_UPDATE_SERVICE_UNAVAILABLE", verified_basis(("A330",)))

        try:
            result = await hot_update.reload_modules(
                governance=self.app.governance,
                modules=list(update_set),
            )
        except Exception as error:
            self._record_operation_failure(operation_id, error)
            return refusal_outcome("EXECUTION_FAILED", verified_basis(("A330",)))

        # Record operation
        self._certified_update_operations[operation_id] = {
            "operation_id": operation_id,
            "update_type": update_type,
            "update_set": list(update_set),
            "artifact_hashes": dict(artifact_hashes),
            "target_generation": request.payload.get("target_generation", ""),
            "decision": "authorized",
            "execution_result": {
                "ok": result.ok,
                "reloaded": list(result.reloaded),
                "skipped": list(result.skipped),
                "errors": list(result.errors),
            },
            "authorized_at": self._iso_now(),
            "terminal_status": "global-success" if result.ok else "failed-isolated",
        }

        return accepted_outcome(
            {
                "repair_decision": "authorized",
                "update_type": update_type,
                "operation_id": operation_id,
                "execution_result": {
                    "ok": result.ok,
                    "reloaded": list(result.reloaded),
                    "skipped": list(result.skipped),
                    "errors": list(result.errors),
                },
            },
            verified_basis(("A330",)),
        )

    def _record_operation_failure(self, operation_id: str, error: Exception) -> None:
        self._certified_update_operations[operation_id] = {
            "operation_id": operation_id,
            "decision": "authorized",
            "error": f"{type(error).__name__}: {error}",
            "authorized_at": self._iso_now(),
            "terminal_status": "failed-isolated",
        }

    def record_certified_update_status(
        self, operation_id: str, terminal_status: str, **detail: Any
    ) -> bool:
        """Update tracked certified update's terminal status."""
        record = self._certified_update_operations.get(operation_id)
        if record is None:
            return False
        if terminal_status not in _A330_TERMINAL_STATUSES:
            return False
        if record.get("terminal_status") in _A330_TERMINAL_STATUSES:
            return False
        record["terminal_status"] = terminal_status
        record["updated_at"] = self._iso_now()
        record.update(detail)
        return True

    def certified_update_status(self) -> dict[str, Any]:
        """Read-only lifecycle status of tracked operations."""
        active = [
            record
            for record in self._certified_update_operations.values()
            if record.get("terminal_status") not in _A330_TERMINAL_STATUSES
        ]
        recent = [
            record
            for record in self._certified_update_operations.values()
            if record.get("terminal_status") in _A330_TERMINAL_STATUSES
        ][-8:]
        return {
            "active_operations": active,
            "recent_terminal": recent,
            "total_tracked": len(self._certified_update_operations),
        }