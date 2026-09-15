"""Repair coordinator governed helpers (A185 split).

Contains the confirmation recording and crash repair execution helpers
extracted from request_governed_repair.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .repair_coordinator_types import _iso_now


def _record_awaiting_confirmation(
    request_record: dict[str, Any],
    decision_proof: dict[str, Any],
    request_id: str,
    failure_code: str,
    owner: str,
    project_root: Any,
    read_requests_fn: Any,
    write_requests_fn: Any,
) -> None:
    """Record an awaiting-confirmation request and pending action."""
    request_record["status"] = "awaiting-confirmation"
    request_record["awaiting_confirmation_at"] = _iso_now()
    request_record["classified"] = {
        "error_type": str(
            decision_proof.get("error_type")
            or (decision_proof.get("diagnosis") or {}).get("error_type")
            or ""
        ),
        "target_file": str(
            (decision_proof.get("diagnosis") or {}).get("file") or ""
        ),
        "action": str(
            (decision_proof.get("diagnosis") or {}).get("action") or ""
        ),
    }
    requests = read_requests_fn()
    requests.append(request_record)
    write_requests_fn(requests)
    try:
        from core_system.auto_action_policy import (
            CONFIRMATION_TTL_SECONDS,
            record_pending_action,
        )

        classified = request_record.get("classified") or {}
        expires_at = (
            datetime.now(timezone.utc)
            + timedelta(seconds=CONFIRMATION_TTL_SECONDS)
        ).isoformat()
        failure_code_str = str(failure_code or "fault")
        target_file = str(classified.get("target_file") or "")
        record_pending_action(
            project_root,
            kind="repair",
            summary=(
                f"{failure_code_str}"
                f" ({classified.get('error_type') or 'unknown'})"
            ),
            detail={
                "request_id": request_id,
                "failure_code": failure_code_str,
                "owner": owner,
                "classified": classified,
                "requested_at": request_record["requested_at"],
            },
            action_id=f"repair-{request_id}",
            binding={
                "fault_id": request_id,
                "scope": target_file or failure_code_str,
                "target": target_file or failure_code_str,
                "proposed_method": (
                    str(classified.get("action") or "")
                    or "targeted-source-repair"
                ),
                "risk": str(decision_proof.get("severity") or "unclassified"),
                "rollback": (
                    "governed repair backup + independent verification; "
                    "failed verification rolls back"
                ),
                "expires_at": expires_at,
            },
        )
    except Exception:
        pass


def _execute_crash_repair(
    request_record: dict[str, Any],
    request_id: str,
    repair_executor: Any,
    report: dict[str, Any],
    read_requests_fn: Any,
    write_requests_fn: Any,
) -> None:
    """Execute a crash repair and update the request record."""
    request_record["status"] = "executing"
    requests = read_requests_fn()
    requests.append(request_record)
    write_requests_fn(requests)

    try:
        result = repair_executor()  # type: ignore[misc]
        report["ok"] = bool(result.get("ok")) if isinstance(result, dict) else True
        report["result"] = result
        request_record["status"] = "completed" if report["ok"] else "failed"
        request_record["completed_at"] = _iso_now()
    except Exception as error:
        report["ok"] = False
        report["error"] = f"{type(error).__name__}: {error}"
        request_record["status"] = "failed"
        request_record["error"] = report["error"]
        request_record["completed_at"] = _iso_now()

    # Update the request record in the information layer.
    requests = read_requests_fn()
    for i, req in enumerate(requests):
        if req.get("request_id") == request_id:
            requests[i] = request_record
            break
    write_requests_fn(requests)


__all__ = [
    "_record_awaiting_confirmation",
    "_execute_crash_repair",
]
