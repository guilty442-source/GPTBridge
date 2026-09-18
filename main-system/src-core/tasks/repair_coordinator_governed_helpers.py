"""Repair coordinator governed helpers (A185 split).

Contains the crash repair execution helper extracted from
request_governed_repair.
"""
from __future__ import annotations

from typing import Any

from .repair_coordinator_types import _iso_now


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
    "_execute_crash_repair",
]
