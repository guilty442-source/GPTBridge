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

A mutation executes either under an enabled standing switch or under a
valid, unexpired, evidence-matching, single-use item permission. Detection
and classification are unaffected.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from .auto_action_policy import (
    compute_action_digest,
    switch_enabled_for_kind,
    switch_for_kind,
    update_pending_action_status,
)
from .sovereign_utils import _iso_now
from .confirmation_service_execution import (
    _execute_repair,
    _execute_system_modification,
    _execute_update,
)
from .confirmation_service_helpers import (
    CONFIRMATION_AUDIT_RELATIVE,
    _audit,
    _confirmation_expired,
    _confirmation_of,
    _expired,
    _find_action,
    _other_action_executing,
    _project_root,
    _refresh_remaining_evidence,
    _result,
)


# ----------------------------------------------------------------------
# Xingcheng auxiliary: record / revoke the user confirmation
# ----------------------------------------------------------------------


async def record_confirmation(
    app: Any,
    action_id: str,
    *,
    confirmation_id: str = "",
    permission_mode: str = "standing-switch",
) -> dict[str, Any]:
    """Record a single-use user confirmation for one pending action.

    Does NOT execute anything: the synchronization domain executes the
    approved action.  Both the capability switch and the concrete
    confirmation remains required at execution time. ``single-item`` is an
    explicit one-shot permission and therefore does not require or modify the
    standing automation switch.
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
    early, kind = _confirmation_prechecks(action, action_id)
    if early is not None:
        return early
    normalized_mode = str(permission_mode or "standing-switch").strip()
    if normalized_mode not in {"standing-switch", "single-item"}:
        return _result(False, error_code="PERMISSION_MODE_UNKNOWN", message="unknown permission mode")
    guard, current_digest = _confirmation_guard(
        project_root, action, action_id, kind, normalized_mode
    )
    if guard is not None:
        return guard
    return _record_confirmed(
        project_root, action, action_id, kind, confirmation_id, current_digest,
        normalized_mode,
    )


def _confirmation_prechecks(
    action: dict[str, Any] | None, action_id: str
) -> tuple[dict[str, Any] | None, str]:
    if action is None:
        return _result(
            False,
            error_code="ACTION_NOT_FOUND",
            message=f"no pending action with id {action_id}",
        ), None
    kind = str(action.get("kind") or "")
    if not switch_for_kind(kind):
        return _result(
            False,
            error_code="ACTION_KIND_UNKNOWN",
            message=f"unknown pending action kind: {kind}",
            action_id=action_id,
        ), None
    if action.get("status") == "confirmed":
        existing = _confirmation_of(action)
        return _result(
            True,
            action_id=action_id,
            kind=kind,
            confirmation_id=str(existing.get("confirmation_id") or ""),
            status="confirmed",
            idempotent=True,
        ), kind
    if action.get("status") != "awaiting-confirmation":
        return _result(
            False,
            error_code="ACTION_NOT_PENDING",
            message=f"action status is {action.get('status')}",
            action_id=action_id,
        ), None
    return None, kind


def _confirmation_guard(
    project_root: Path,
    action: dict[str, Any],
    action_id: str,
    kind: str,
    permission_mode: str,
) -> tuple[dict[str, Any] | None, str | None]:
    if permission_mode != "single-item" and not switch_enabled_for_kind(kind):
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
            message="請啟用對應全域開關，或使用單項許可。",
            action_id=action_id,
            status="awaiting-confirmation",
        ), None
    if _expired(action):
        update_pending_action_status(project_root, action_id, "expired")
        return _result(
            False,
            error_code="CONFIRMATION_EXPIRED",
            message="確認已到期，請等待系統重新產生待確認項目。",
            action_id=action_id,
        ), None
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
        ), None
    return None, current_digest


def _record_confirmed(
    project_root: Path,
    action: dict[str, Any],
    action_id: str,
    kind: str,
    confirmation_id: str,
    current_digest: str,
    permission_mode: str,
) -> dict[str, Any]:
    confirmation_id = str(confirmation_id or "").strip() or uuid.uuid4().hex
    confirmation = {
        "confirmation_id": confirmation_id,
        "confirmed_at": _iso_now(),
        "actor": "authenticated-ui",
        "evidence_digest": current_digest,
        "expires_at": str(action.get("expires_at") or ""),
        "single_use": True,
        "permission_mode": permission_mode,
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
            "permission_mode": permission_mode,
        },
    )
    return _result(
        True,
        action_id=action_id,
        kind=kind,
        confirmation_id=confirmation_id,
        status="confirmed",
        expires_at=confirmation["expires_at"],
        permission_mode=permission_mode,
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
        return _result(False, error_code="MISSING_FIELDS", message="action_id and confirmation_id are required")
    project_root = _project_root(app)
    action = _find_action(project_root, action_id)
    if action is None:
        return _result(False, error_code="ACTION_NOT_FOUND", message=f"no pending action with id {action_id}")
    confirmation = _confirmation_of(action)
    if not confirmation or str(confirmation.get("confirmation_id") or "") != confirmation_id:
        return _result(False, error_code="CONFIRMATION_NOT_FOUND", message="no recorded confirmation matches this action", action_id=action_id)
    if action.get("status") not in ("confirmed", "awaiting-confirmation"):
        return _result(False, error_code="CONFIRMATION_NOT_REVOCABLE", message=f"action status is {action.get('status')}", action_id=action_id)
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


async def deny_pending_action(app: Any, action_id: str) -> dict[str, Any]:
    """Record an explicit user denial for exactly one pending action."""
    action_id = str(action_id or "").strip()
    if not action_id:
        return _result(False, error_code="MISSING_ACTION_ID", message="action_id is required")
    project_root = _project_root(app)
    action = _find_action(project_root, action_id)
    if action is None:
        return _result(False, error_code="ACTION_NOT_FOUND", message=f"no pending action with id {action_id}")
    if action.get("status") != "awaiting-confirmation":
        return _result(False, error_code="ACTION_NOT_PENDING", message=f"action status is {action.get('status')}")
    update_pending_action_status(
        project_root,
        action_id,
        "denied",
        denied_at=_iso_now(),
        denied_by="authenticated-ui",
    )
    _audit(project_root, {"event": "single-item-denied", "action_id": action_id})
    return _result(True, action_id=action_id, status="denied")


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
        return _result(False, error_code="MISSING_FIELDS", message="action_id and confirmation_id are required")
    project_root = _project_root(app)
    action = _find_action(project_root, action_id)
    if action is None:
        return _result(False, error_code="ACTION_NOT_FOUND", message=f"no pending action with id {action_id}")
    kind = str(action.get("kind") or "")
    if not switch_for_kind(kind):
        return _result(False, error_code="ACTION_KIND_UNKNOWN", message=f"unknown pending action kind: {kind}", action_id=action_id)
    early, confirmation = _execution_prechecks(
        project_root, action, action_id, kind, confirmation_id
    )
    if early is not None:
        return early

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
    elif kind == "system-modification":
        outcome = await _execute_system_modification(
            app, project_root, action
        )
    else:
        outcome = await _execute_update(app, project_root, action)

    return _finalize_execution(
        project_root, action_id, kind, confirmation_id, confirmation, outcome
    )


def _execution_prechecks(
    project_root: Path,
    action: dict[str, Any],
    action_id: str,
    kind: str,
    confirmation_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    confirmation = _confirmation_of(action)
    early = _execution_status_checks(action, action_id, confirmation, confirmation_id)
    if early is not None:
        return early, None
    early = _execution_guard_checks(project_root, action, action_id, kind, confirmation)
    if early is not None:
        return early, None
    return None, confirmation


def _execution_status_checks(
    action: dict[str, Any],
    action_id: str,
    confirmation: dict[str, Any],
    confirmation_id: str,
) -> dict[str, Any] | None:
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
    return None


def _execution_guard_checks(
    project_root: Path,
    action: dict[str, Any],
    action_id: str,
    kind: str,
    confirmation: dict[str, Any],
) -> dict[str, Any] | None:
    if (
        str(confirmation.get("permission_mode") or "standing-switch") != "single-item"
        and not switch_enabled_for_kind(kind)
    ):
        return _result(
            False,
            error_code="SWITCH_DISABLED",
            message="請啟用對應全域開關，或使用單項許可。",
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
    return None


def _finalize_execution(
    project_root: Path,
    action_id: str,
    kind: str,
    confirmation_id: str,
    confirmation: dict[str, Any],
    outcome: dict[str, Any],
) -> dict[str, Any]:
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
            # Bounded executor payloads (e.g. config_value before_value)
            # must persist — rollback_of reads them from this record.
            **(outcome.get("result") or {}),
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


__all__ = [
    "CONFIRMATION_AUDIT_RELATIVE",
    "deny_pending_action",
    "execute_approved",
    "record_confirmation",
    "revoke_confirmation",
]
