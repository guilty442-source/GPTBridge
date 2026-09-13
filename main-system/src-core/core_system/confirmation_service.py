"""Per-item user confirmation service for the assistant panel.

The assistant (Xingcheng) panel lists pending actions recorded by the
automatic-repair/update freeze and confirms them one by one.  Per the user
directive, nothing executes until BOTH conditions hold:

  1. the operator release switch ``user_confirmation_release`` is enabled
     (global 「放行」), and
  2. the user explicitly confirms that single item in the panel.

While the release switch is off every confirmation is refused with
``RELEASE_NOT_GRANTED`` and the item stays ``awaiting-confirmation`` —
the wiring is live but nothing is released.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .auto_action_policy import (
    read_pending_actions,
    update_pending_action_status,
    user_confirmation_release_allowed,
)
from .sovereign_utils import _iso_now


def _result(ok: bool, **fields: Any) -> dict[str, Any]:
    return {"ok": ok, **fields}


async def confirm_action(app: Any, action_id: str) -> dict[str, Any]:
    """Confirm one pending action (repair or update) by id."""
    action_id = str(action_id or "").strip()
    if not action_id:
        return _result(False, error_code="MISSING_ACTION_ID", message="action_id is required")

    project_root = Path(getattr(app, "project_root", "") or ".")
    action = next(
        (
            item
            for item in read_pending_actions(project_root)
            if item.get("action_id") == action_id
        ),
        None,
    )
    if action is None:
        return _result(
            False,
            error_code="ACTION_NOT_FOUND",
            message=f"no pending action with id {action_id}",
        )
    if action.get("status") != "awaiting-confirmation":
        return _result(
            False,
            error_code="ACTION_NOT_PENDING",
            message=f"action status is {action.get('status')}",
        )
    if not user_confirmation_release_allowed():
        return _result(
            False,
            error_code="RELEASE_NOT_GRANTED",
            message="尚未放行：全域凍結中，確認後仍未允許執行。",
            action_id=action_id,
            status="awaiting-confirmation",
        )

    kind = str(action.get("kind") or "")
    if kind == "repair":
        return await _confirm_repair(app, project_root, action)
    if kind == "update":
        return await _confirm_update(app, project_root, action)
    return _result(
        False,
        error_code="ACTION_KIND_UNKNOWN",
        message=f"unknown pending action kind: {kind}",
        action_id=action_id,
    )


async def _confirm_repair(
    app: Any, project_root: Path, action: dict[str, Any]
) -> dict[str, Any]:
    from tasks.repair_coordinator import get_repair_coordinator

    action_id = str(action.get("action_id") or "")
    request_id = str((action.get("detail") or {}).get("request_id") or "")
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
    try:
        result = decision_sovereign.decide_and_route_repair(
            classified, user_confirmed=True
        )
    except Exception as error:
        detail = f"{type(error).__name__}: {error}"
        coordinator.mark_request_status(request_id, "failed", error=detail)
        update_pending_action_status(
            project_root, action_id, "failed", error=detail
        )
        return _result(
            False,
            error_code="REPAIR_EXECUTION_FAILED",
            message=detail,
            action_id=action_id,
        )

    ok = bool(result.get("ok"))
    decision = str(result.get("decision") or ("completed" if ok else "failed"))
    coordinator.acknowledge_request(request_id, decision=decision, ok=ok)
    update_pending_action_status(
        project_root,
        action_id,
        "executed" if ok else "failed",
        decision=decision,
        result={
            "decision": decision,
            "execution": result.get("execution", {}),
            "verification": result.get("verification", {}),
        },
    )
    return _result(
        ok,
        action_id=action_id,
        request_id=request_id,
        decision=decision,
        result=result,
    )


async def _confirm_update(
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
        detail_text = f"{type(error).__name__}: {error}"
        update_pending_action_status(
            project_root, action_id, "failed", error=detail_text
        )
        return _result(
            False,
            error_code="UPDATE_EXECUTION_FAILED",
            message=detail_text,
            action_id=action_id,
        )

    update_pending_action_status(
        project_root,
        action_id,
        "executed" if accepted else "failed",
        handover="prepared" if accepted else "deferred",
    )
    return _result(
        bool(accepted),
        action_id=action_id,
        handover="prepared" if accepted else "deferred",
    )


__all__ = ["confirm_action"]
