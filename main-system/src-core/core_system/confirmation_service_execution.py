"""Confirmation service — execution bodies.

Extracted from confirmation_service.py: the _execute_repair and
_execute_update functions that run the approved action through the
governance repair/update chain.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .sovereign_utils import _iso_now


def _result(ok: bool, **fields: Any) -> dict[str, Any]:
    return {"ok": ok, **fields}


async def _execute_repair(
    app: Any, project_root: Path, action: dict[str, Any]
) -> dict[str, Any]:
    from tasks.repair_coordinator import get_repair_coordinator

    action_id = str(action.get("action_id") or "")
    detail = action.get("detail") or {}
    request_id = str(action.get("fault_id") or detail.get("request_id") or "")
    coordinator = get_repair_coordinator()
    if coordinator is None or not request_id:
        return _result(
            False,
            error_code="REPAIR_REQUEST_UNAVAILABLE",
            message="repair coordinator or request id is unavailable",
            action_id=action_id,
        )
    request = coordinator.get_request(request_id)
    if request is None or request.get("status") != "awaiting-confirmation":
        return _result(
            False,
            error_code="REPAIR_REQUEST_NOT_PENDING",
            message="repair request is missing or not awaiting confirmation",
            action_id=action_id,
        )

    maintenance = getattr(app, "maintenance_sovereign", None)
    decision_sovereign = getattr(app, "decision_sovereign", None)
    if maintenance is None or decision_sovereign is None:
        return _result(
            False,
            error_code="REPAIR_CHAIN_UNAVAILABLE",
            message="maintenance or decision sovereign is unavailable",
            action_id=action_id,
        )

    classified = request.get("classified") or maintenance._classify_health_signal(
        request.get("decision_proof") or {}
    )
    coordinator.mark_request_status(
        request_id, "executing", executing_at=_iso_now()
    )
    return _route_repair_decision(
        coordinator, request_id, decision_sovereign, classified, action_id
    )


def _route_repair_decision(
    coordinator: Any,
    request_id: str,
    decision_sovereign: Any,
    classified: Any,
    action_id: str,
) -> dict[str, Any]:
    try:
        result = decision_sovereign.decide_and_route_repair(
            classified, user_confirmed=True
        )
    except Exception as error:
        detail_text = f"{type(error).__name__}: {error}"
        coordinator.mark_request_status(request_id, "failed", error=detail_text)
        return _result(
            False,
            error_code="REPAIR_EXECUTION_FAILED",
            message=detail_text,
            action_id=action_id,
        )

    ok = bool(result.get("ok"))
    decision = str(result.get("decision") or ("completed" if ok else "failed"))
    coordinator.acknowledge_request(request_id, decision=decision, ok=ok)
    return _result(
        ok,
        action_id=action_id,
        fault_id=request_id,
        decision=decision,
        result=result,
    )


async def _execute_update(
    app: Any, project_root: Path, action: dict[str, Any]
) -> dict[str, Any]:
    action_id = str(action.get("action_id") or "")
    detail = action.get("detail") or {}
    watcher = getattr(app, "hot_reload_watcher", None)
    if watcher is None:
        return _result(
            False,
            error_code="HOT_RELOAD_WATCHER_UNAVAILABLE",
            message="hot reload watcher is unavailable",
            action_id=action_id,
        )

    changed_paths = [
        str(path) for path in (detail.get("changed_paths") or []) if path
    ]
    if not changed_paths:
        for module_name in detail.get("modules") or []:
            module = sys.modules.get(str(module_name))
            file_path = getattr(module, "__file__", None)
            if file_path:
                changed_paths.append(str(file_path))
    if not changed_paths:
        return _result(
            False,
            error_code="UPDATE_TARGETS_UNAVAILABLE",
            message="pending update has no resolvable changed paths",
            action_id=action_id,
        )

    try:
        accepted = await watcher._maybe_reload(
            changed_paths, user_confirmed=True
        )
    except Exception as error:
        return _result(
            False,
            error_code="UPDATE_EXECUTION_FAILED",
            message=f"{type(error).__name__}: {error}",
            action_id=action_id,
        )

    return _result(
        bool(accepted),
        action_id=action_id,
        update_id=str(action.get("update_id") or action_id),
        handover="prepared" if accepted else "deferred",
    )
