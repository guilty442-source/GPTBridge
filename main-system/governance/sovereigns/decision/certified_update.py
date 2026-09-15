"""Decision Sovereign — Certified Update Handling (A152/A154/A330).

Validates certification proof, authorizes updates, delegates A330 execution
to synchronization-sovereign. Tracks lifecycle but never executes.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .._base import SovereignBase, SovereignOutcome, SovereignRequest
from core_system.codex_decision import accepted_outcome, refusal_outcome
from core_system.sovereign_utils import _iso_now

# A330 terminal statuses
_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "global-success",
    "failed-isolated",
    "rolled-back",
    "partial-deferred",
})


class DecisionCertifiedUpdateMixin:
    """Certified update authorization and lifecycle tracking (A330)."""

    _certified_updates: dict[str, dict[str, Any]]
    workspace_root: Path
    app: Any

    async def _adjudicate_certified_update(
        self, request: SovereignRequest
    ) -> SovereignOutcome:
        """A152/A154/A330: certified update decision."""
        error, fields = self._validate_certified_update_request(request)
        if error is not None:
            return error
        update_type, update_set, artifact_hashes, operation_id = fields

        # Idempotent replay guard
        if operation_id in self._certified_updates:
            existing = self._certified_updates[operation_id]
            existing_status = existing.get("terminal_status", "")
            if existing_status in _TERMINAL_STATUSES:
                return accepted_outcome(
                    {
                        "repair_decision": "authorized",
                        "update_type": update_type,
                        "operation_id": operation_id,
                        "terminal_status": existing_status,
                        "idempotent_replay": True,
                    },
                    self.verified_basis("A152", "A154", "A330"),
                )
            return refusal_outcome(
                "OPERATION_IN_FLIGHT", self.verified_basis("A152", "A330")
            )

        # Delegate A330 execution to synchronization-sovereign
        return await self._delegate_certified_update(
            request, update_type, update_set, artifact_hashes, operation_id
        )

    def _validate_certified_update_request(
        self, request: SovereignRequest
    ) -> tuple[SovereignOutcome | None, tuple | None]:
        update_type = request.payload.get("update_type")
        if not update_type:
            return refusal_outcome(
                "MISSING_UPDATE_TYPE", self.verified_basis("A152", "A330")
            ), None
        if request.payload.get("certified") is not True:
            return refusal_outcome(
                "CERTIFICATION_MISSING", self.verified_basis("A152", "A330")
            ), None
        update_set = request.payload.get("update_set")
        if not isinstance(update_set, (list, tuple)) or not update_set:
            return refusal_outcome(
                "EMPTY_UPDATE_SET", self.verified_basis("A152", "A330")
            ), None
        artifact_hashes = request.payload.get("artifact_hashes")
        if not isinstance(artifact_hashes, dict) or not artifact_hashes:
            return refusal_outcome(
                "MISSING_ARTIFACT_HASHES", self.verified_basis("A152", "A330")
            ), None
        operation_id = str(request.payload.get("operation_id") or "")
        if not operation_id:
            return refusal_outcome(
                "MISSING_OPERATION_ID", self.verified_basis("A152", "A330")
            ), None
        return None, (update_type, update_set, artifact_hashes, operation_id)

    async def _delegate_certified_update(
        self,
        request: SovereignRequest,
        update_type: Any,
        update_set: Any,
        artifact_hashes: dict,
        operation_id: str,
    ) -> SovereignOutcome:
        sync_outcome = await self.delegate_to(
            "synchronization-sovereign",
            SovereignRequest(
                intent="A330.certified-update",
                subject=request.subject,
                requester=request.requester,
                payload=request.payload,
            ),
        )
        if not sync_outcome.accepted:
            if sync_outcome.refusal and sync_outcome.refusal.reason_code == (
                "TARGET_SOVEREIGN_UNAVAILABLE"
            ):
                return refusal_outcome(
                    "SYNCHRONIZATION_SOVEREIGN_UNAVAILABLE",
                    self.verified_basis("A152", "A330", "A301"),
                )
            return sync_outcome

        # Record for lifecycle tracking
        self._certified_updates[operation_id] = {
            "operation_id": operation_id,
            "update_type": update_type,
            "update_set": list(update_set),
            "artifact_hashes": dict(artifact_hashes),
            "target_generation": request.payload.get("target_generation", ""),
            "decision": "authorized",
            "sync_authorization": sync_outcome.result,
            "authorized_at": _iso_now(),
            "terminal_status": "authorized",
        }

        return accepted_outcome(
            {
                "repair_decision": "authorized",
                "update_type": update_type,
                "operation_id": operation_id,
                "route": "decision-sovereign > synchronization-sovereign(A330) > governed-executor",
                "sync_authorization": sync_outcome.result,
                "forbidden": "decision-sovereign-direct-execution",
            },
            self.verified_basis("A152", "A154", "A330", "A63", "A64"),
        )

    def record_certified_update_status(
        self, operation_id: str, terminal_status: str, **detail: Any
    ) -> bool:
        """Update tracked certified update's terminal status."""
        record = self._certified_updates.get(operation_id)
        if record is None:
            return False
        if terminal_status not in _TERMINAL_STATUSES:
            return False
        if record.get("terminal_status") in _TERMINAL_STATUSES:
            return False
        record["terminal_status"] = terminal_status
        record["updated_at"] = _iso_now()
        record.update(detail)
        return True

    def certified_update_status(self) -> dict[str, Any]:
        """Read-only lifecycle status of tracked operations."""
        active = [
            record
            for record in self._certified_updates.values()
            if record.get("terminal_status") not in _TERMINAL_STATUSES
        ]
        recent = [
            record
            for record in self._certified_updates.values()
            if record.get("terminal_status") in _TERMINAL_STATUSES
        ][-8:]
        return {
            "active_operations": active,
            "recent_terminal": recent,
            "total_tracked": len(self._certified_updates),
        }

    def _reconcile_certified_updates(self) -> None:
        """Close A330 feedback loop when watcher missed a report."""
        active = {
            operation_id
            for operation_id, record in self._certified_updates.items()
            if record.get("terminal_status") not in _TERMINAL_STATUSES
        }
        if not active:
            return
        request_path = (
            self.workspace_root
            / "main-system"
            / "runtime"
            / "state"
            / "backend-update-request.json"
        )
        try:
            payload = json.loads(request_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        operation = (
            payload.get("last_update_operation")
            if isinstance(payload, dict)
            else None
        )
        if not isinstance(operation, dict):
            return
        operation_id = str(operation.get("operation_id") or "")
        status = str(operation.get("status") or "")
        if operation_id in active and status in _TERMINAL_STATUSES:
            self.record_certified_update_status(
                operation_id,
                status,
                reconciled_from="backend-update-request.json",
            )