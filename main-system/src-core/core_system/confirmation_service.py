"""Per-action user confirmation and approved-action execution (codex A366).

Implements the execution gate of
``xingcheng-auxiliary-repair-update-user-switch-confirmation-and-fault-
cardinality`` with the registered command-code split:

  * Xingcheng auxiliary (user-facing control surface):
      - ``xingcheng-set-repair-release`` / ``xingcheng-set-update-release``
        set the persisted switches;
      - ``xingcheng-confirm-automatic-repair`` /
        ``xingcheng-confirm-automatic-update`` record one single-use,
        non-transferable user confirmation bound to the concrete action;
      - ``xingcheng-revoke-automatic-*-confirmation`` revoke a recorded
        confirmation before it is consumed.
  * Synchronization domain (execution):
      - ``sync-execute-approved-automatic-repair`` /
        ``sync-execute-approved-automatic-update`` execute an approved
        action exactly once for its confirmation id.

A mutation executes only when its switch is enabled AND the concrete
pending action carries a valid, unexpired, evidence-matching, single-use
confirmation.  Detection and classification are unaffected.
"""

from __future__ import annotations

import json
import sys
import uuid
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


def _find_action(project_root: Path, action_id: str) -> dict[str, Any] | None:
    return next(
        (
            item
            for item in read_pending_actions(project_root)
            if item.get("action_id") == action_id
        ),
        None,
    )


def _expired(action: dict[str, Any]) -> bool:
    raw = str(action.get("expires_at") or "").strip()
    if not raw:
        return False
    try:
        expiry = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) > expiry


def _confirmation_of(action: dict[str, Any]) -> dict[str, Any]:
    value = action.get("confirmation")
    return value if isinstance(value, dict) else {}


def _confirmation_expired(confirmation: dict[str, Any]) -> bool:
    raw = str(confirmation.get("expires_at") or "").strip()
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


# ----------------------------------------------------------------------
# Xingcheng auxiliary: record / revoke the user confirmation
# ----------------------------------------------------------------------


async def record_confirmation(
    app: Any,
    action_id: str,
    *,
    confirmation_id: str = "",
) -> dict[str, Any]:
    """Record a single-use user confirmation for one pending action.

    Does NOT execute anything: the synchronization domain executes the
    approved action.  Both the capability switch and the concrete
    confirmation remain required at execution time.
    """
    action_id = str(action_id or "").strip()
    if not action_id:
        return _result(
            False, error_code="MISSING_ACTION_ID", message="action_id is required"
        )
    project_root = _project_root(app)
    action = _find_action(project_root, action_id)
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
    kind = str(action.get("kind") or "")
    if not switch_for_kind(kind):
        return _result(
            False,
            error_code="ACTION_KIND_UNKNOWN",
            message=f"unknown pending action kind: {kind}",
            action_id=action_id,
        )
    if action.get("status") == "confirmed":
        existing = _confirmation_of(action)
        return _result(
            True,
            action_id=action_id,
            kind=kind,
            confirmation_id=str(existing.get("confirmation_id") or ""),
            status="confirmed",
            idempotent=True,
        )
    if action.get("status") != "awaiting-confirmation":
        return _result(
            False,
            error_code="ACTION_NOT_PENDING",
            message=f"action status is {action.get('status')}",
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
            message="對應的自動執行開關未啟用；開關與逐筆確認必須同時成立。",
            action_id=action_id,
            status="awaiting-confirmation",
        )
    if _expired(action):
        update_pending_action_status(project_root, action_id, "expired")
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
        return _result(
            False,
            error_code="EVIDENCE_CHANGED",
            message="計畫或證據已變更，舊確認失效；請重新確認。",
            action_id=action_id,
        )

    confirmation_id = str(confirmation_id or "").strip() or uuid.uuid4().hex
    confirmation = {
        "confirmation_id": confirmation_id,
        "confirmed_at": _iso_now(),
        "actor": "authenticated-ui",
        "evidence_digest": current_digest,
        "expires_at": str(action.get("expires_at") or ""),
        "single_use": True,
        "consumed": False,
    }
    update_pending_action_status(
        project_root, action_id, "confirmed", confirmation=confirmation
    )
    _audit(
        project_root,
        {
            "event": "confirmation-recorded",
            "action_id": action_id,
            "kind": kind,
            "confirmation_id": confirmation_id,
            "evidence_digest": current_digest,
        },
    )
    return _result(
        True,
        action_id=action_id,
        kind=kind,
        confirmation_id=confirmation_id,
        status="confirmed",
        expires_at=confirmation["expires_at"],
    )


async def revoke_confirmation(
    app: Any,
    action_id: str,
    confirmation_id: str,
) -> dict[str, Any]:
    """Revoke a recorded confirmation before it is consumed."""
    action_id = str(action_id or "").strip()
    confirmation_id = str(confirmation_id or "").strip()
    if not action_id or not confirmation_id:
        return _result(
            False,
            error_code="MISSING_FIELDS",
            message="action_id and confirmation_id are required",
        )
    project_root = _project_root(app)
    action = _find_action(project_root, action_id)
    if action is None:
        return _result(
            False,
            error_code="ACTION_NOT_FOUND",
            message=f"no pending action with id {action_id}",
        )
    confirmation = _confirmation_of(action)
    if not confirmation or str(confirmation.get("confirmation_id") or "") != confirmation_id:
        return _result(
            False,
            error_code="CONFIRMATION_NOT_FOUND",
            message="no recorded confirmation matches this action",
            action_id=action_id,
        )
    if action.get("status") not in ("confirmed", "awaiting-confirmation"):
        return _result(
            False,
            error_code="CONFIRMATION_NOT_REVOCABLE",
            message=f"action status is {action.get('status')}",
            action_id=action_id,
        )
    update_pending_action_status(
        project_root,
        action_id,
        "awaiting-confirmation",
        confirmation=None,
        revoked_at=_iso_now(),
    )
    _audit(
        project_root,
        {
            "event": "confirmation-revoked",
            "action_id": action_id,
            "confirmation_id": confirmation_id,
        },
    )
    return _result(True, action_id=action_id, status="awaiting-confirmation")


# ----------------------------------------------------------------------
# Synchronization domain: execute the approved action
# ----------------------------------------------------------------------


async def execute_approved(
    app: Any,
    action_id: str,
    confirmation_id: str,
) -> dict[str, Any]:
    """Execute one approved action exactly once for its confirmation id."""
    action_id = str(action_id or "").strip()
    confirmation_id = str(confirmation_id or "").strip()
    if not action_id or not confirmation_id:
        return _result(
            False,
            error_code="MISSING_FIELDS",
            message="action_id and confirmation_id are required",
        )
    project_root = _project_root(app)
    action = _find_action(project_root, action_id)
    if action is None:
        return _result(
            False,
            error_code="ACTION_NOT_FOUND",
            message=f"no pending action with id {action_id}",
        )
    kind = str(action.get("kind") or "")
    if not switch_for_kind(kind):
        return _result(
            False,
            error_code="ACTION_KIND_UNKNOWN",
            message=f"unknown pending action kind: {kind}",
            action_id=action_id,
        )
    confirmation = _confirmation_of(action)
    if (
        action.get("status") in ("executed", "failed")
        and str(confirmation.get("confirmation_id") or "") == confirmation_id
    ):
        return _result(
            True,
            action_id=action_id,
            status=action.get("status"),
            idempotent=True,
            result=action.get("result", {}),
        )
    if action.get("status") != "confirmed" or not confirmation:
        return _result(
            False,
            error_code="NOT_CONFIRMED",
            message="action has no recorded user confirmation",
            action_id=action_id,
        )
    if str(confirmation.get("confirmation_id") or "") != confirmation_id:
        return _result(
            False,
            error_code="CONFIRMATION_MISMATCH",
            message="confirmation id does not match the recorded confirmation",
            action_id=action_id,
        )
    if not switch_enabled_for_kind(kind):
        return _result(
            False,
            error_code="SWITCH_DISABLED",
            message="對應的自動執行開關未啟用；開關與逐筆確認必須同時成立。",
            action_id=action_id,
        )
    if _confirmation_expired(confirmation):
        update_pending_action_status(project_root, action_id, "expired")
        return _result(
            False,
            error_code="CONFIRMATION_EXPIRED",
            message="確認已到期；請重新確認。",
            action_id=action_id,
        )
    current_digest = compute_action_digest(action)
    if str(confirmation.get("evidence_digest") or "") != current_digest:
        update_pending_action_status(
            project_root, action_id, "invalidated", confirmation=None
        )
        return _result(
            False,
            error_code="EVIDENCE_CHANGED",
            message="計畫或證據已變更，確認失效；請重新確認。",
            action_id=action_id,
        )
    if _other_action_executing(project_root, action_id):
        return _result(
            False,
            error_code="EXECUTION_IN_PROGRESS",
            message="另有項目執行中；多筆故障一次只執行一筆。",
            action_id=action_id,
        )

    update_pending_action_status(project_root, action_id, "executing")
    _audit(
        project_root,
        {
            "event": "execution-started",
            "action_id": action_id,
            "kind": kind,
            "confirmation_id": confirmation_id,
        },
    )

    if kind == "repair":
        outcome = await _execute_repair(app, project_root, action)
    else:
        outcome = await _execute_update(app, project_root, action)

    final_status = "executed" if outcome.get("ok") else "failed"
    consumed = {**confirmation, "consumed": True, "consumed_at": _iso_now()}
    update_pending_action_status(
        project_root,
        action_id,
        final_status,
        confirmation=consumed,
        result={
            "error_code": outcome.get("error_code", ""),
            "decision": outcome.get("decision", ""),
            "handover": outcome.get("handover", ""),
        },
    )
    _refresh_remaining_evidence(project_root, action_id)
    _audit(
        project_root,
        {
            "event": "execution-result",
            "action_id": action_id,
            "kind": kind,
            "ok": bool(outcome.get("ok")),
            "error_code": outcome.get("error_code", ""),
        },
    )
    return outcome


# ----------------------------------------------------------------------
# Execution bodies (unchanged governance chain)
# ----------------------------------------------------------------------


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


__all__ = [
    "CONFIRMATION_AUDIT_RELATIVE",
    "execute_approved",
    "record_confirmation",
    "revoke_confirmation",
]
