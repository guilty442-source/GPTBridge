"""Per-action user confirmation service for the Xingcheng assistant panel.

Implements the execution gate of codex A366
(``xingcheng-auxiliary-repair-update-user-switch-confirmation-and-fault-
cardinality``):

  * A mutation may execute only when its corresponding persisted switch
    (``automatic_repair_enabled`` / ``automatic_update_enabled``) is enabled
    AND the user confirms that concrete pending action.
  * Confirmation binds the action id, scope, target, proposed method, risk,
    rollback/fallback, evidence digest and expiry; it is one-time,
    non-transferable and invalid after a material plan/evidence change.
  * Multi-fault execution runs one action at a time; after each action the
    remaining evidence is refreshed so a repair cannot ride a stale
    confirmation.

Detection, classification and evidence collection are unaffected — this
service only governs execution.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .auto_action_policy import (
    compute_action_digest,
    read_pending_actions,
    switch_enabled_for_kind,
    switch_for_kind,
    update_pending_action_status,
)
from .sovereign_utils import _iso_now

CONFIRMATION_AUDIT_RELATIVE = (
    "main-system",
    "runtime",
    "state",
    "confirmation-audit.jsonl",
)


def _result(ok: bool, **fields: Any) -> dict[str, Any]:
    return {"ok": ok, **fields}


def _project_root(app: Any) -> Path:
    raw = getattr(app, "project_root", None)
    if raw:
        return Path(raw)
    # core_system/confirmation_service.py -> src-core -> main-system -> root
    return Path(__file__).resolve().parents[3]


def _audit(project_root: Path, entry: dict[str, Any]) -> None:
    """Append a confirmation audit line (A366 AUDIT, no secrets)."""
    try:
        path = project_root.joinpath(*CONFIRMATION_AUDIT_RELATIVE)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {"timestamp": _iso_now(), **entry},
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    except OSError:
        pass


def _expired(action: dict[str, Any]) -> bool:
    raw = str(action.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        expiry = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) > expiry


def _other_action_executing(project_root: Path, action_id: str) -> bool:
    return any(
        action.get("action_id") != action_id
        and action.get("status") == "executing"
        for action in read_pending_actions(project_root)
    )


def _refresh_remaining_evidence(project_root: Path, executed_id: str) -> None:
    """Recompute evidence digests for actions still awaiting confirmation."""
    actions = read_pending_actions(project_root)
    changed = False
    for action in actions:
        if action.get("action_id") == executed_id:
            continue
        if action.get("status") != "awaiting-confirmation":
            continue
        digest = compute_action_digest(action)
        if action.get("evidence_digest") != digest:
            action["evidence_digest"] = digest
            action["updated_at"] = _iso_now()
            changed = True
    if changed:
        from .auto_action_policy import _write_pending_actions

        _write_pending_actions(project_root, actions)


async def confirm_action(app: Any, action_id: str) -> dict[str, Any]:
    """Confirm one pending action (repair or update) by id."""
    action_id = str(action_id or "").strip()
    if not action_id:
        return _result(
            False, error_code="MISSING_ACTION_ID", message="action_id is required"
        )

    project_root = _project_root(app)
    action = next(
        (
            item
            for item in read_pending_actions(project_root)
            if item.get("action_id") == action_id
        ),
        None,
    )
    _audit(
        project_root,
        {
            "event": "confirmation-prompt",
            "action_id": action_id,
            "actor": "authenticated-ui",
            "found": action is not None,
        },
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

    kind = str(action.get("kind") or "")
    if not switch_for_kind(kind):
        return _result(
            False,
            error_code="ACTION_KIND_UNKNOWN",
            message=f"unknown pending action kind: {kind}",
            action_id=action_id,
        )
    if not switch_enabled_for_kind(kind):
        _audit(
            project_root,
            {
                "event": "confirmation-refused",
                "reason": "switch-disabled",
                "action_id": action_id,
                "kind": kind,
            },
        )
        return _result(
            False,
            error_code="SWITCH_DISABLED",
            message=(
                "對應的自動執行開關未啟用；開關與逐筆確認必須同時成立。"
            ),
            action_id=action_id,
            status="awaiting-confirmation",
        )

    if _expired(action):
        update_pending_action_status(project_root, action_id, "expired")
        _audit(
            project_root,
            {
                "event": "confirmation-refused",
                "reason": "expired",
                "action_id": action_id,
                "kind": kind,
            },
        )
        return _result(
            False,
            error_code="CONFIRMATION_EXPIRED",
            message="確認已到期，請等待系統重新產生待確認項目。",
            action_id=action_id,
        )

    current_digest = compute_action_digest(action)
    stored_digest = str(action.get("evidence_digest") or "").strip()
    if stored_digest and stored_digest != current_digest:
        update_pending_action_status(
            project_root, action_id, "invalidated", evidence_digest=current_digest
        )
        _audit(
            project_root,
            {
                "event": "confirmation-refused",
                "reason": "evidence-changed",
                "action_id": action_id,
                "kind": kind,
            },
        )
        return _result(
            False,
            error_code="EVIDENCE_CHANGED",
            message="計畫或證據已變更，舊確認失效；請重新確認。",
            action_id=action_id,
        )

    if _other_action_executing(project_root, action_id):
        return _result(
            False,
            error_code="EXECUTION_IN_PROGRESS",
            message="另有項目執行中；多筆故障一次只執行一筆。",
            action_id=action_id,
        )

    confirmation = {
        "confirmed_at": _iso_now(),
        "actor": "authenticated-ui",
        "evidence_digest": current_digest,
        "switch": switch_for_kind(kind),
    }
    update_pending_action_status(
        project_root, action_id, "executing", confirmation=confirmation
    )
    _audit(
        project_root,
        {
            "event": "confirmation-accepted",
            "action_id": action_id,
            "kind": kind,
            "evidence_digest": current_digest,
        },
    )

    if kind == "repair":
        result = await _execute_repair(app, project_root, action)
    else:
        result = await _execute_update(app, project_root, action)

    final_status = "executed" if result.get("ok") else "failed"
    update_pending_action_status(
        project_root,
        action_id,
        final_status,
        result={
            "error_code": result.get("error_code", ""),
            "decision": result.get("decision", ""),
            "handover": result.get("handover", ""),
        },
    )
    _refresh_remaining_evidence(project_root, action_id)
    _audit(
        project_root,
        {
            "event": "confirmation-result",
            "action_id": action_id,
            "kind": kind,
            "ok": bool(result.get("ok")),
            "error_code": result.get("error_code", ""),
        },
    )
    return result


async def _execute_repair(
    app: Any, project_root: Path, action: dict[str, Any]
) -> dict[str, Any]:
    from tasks.repair_coordinator import get_repair_coordinator

    action_id = str(action.get("action_id") or "")
    detail = action.get("detail") or {}
    request_id = str(
        action.get("fault_id") or detail.get("request_id") or ""
    )
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


__all__ = ["confirm_action", "CONFIRMATION_AUDIT_RELATIVE"]
