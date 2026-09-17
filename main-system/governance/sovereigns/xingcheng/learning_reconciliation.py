"""Learning reconciliation mixin (A185 split; A485 — learning sub-sovereign).

Moved with the learning-sub-sovereign into the 星澄 owner package
(``governance/sovereigns/xingcheng``) per A485
(learning-sub-sovereign-transfer-to-xingcheng).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .learning_constants import (
    RECONCILIATION_AUDIT_RELATIVE,
    NON_ACTIONABLE_REMEDY,
    ORPHANED_REQUEST_GRACE_SECONDS as _ORPHANED_REQUEST_GRACE_SECONDS,
)


class LearningReconciliationMixin:
    """Fault message reconciliation and learning."""

    ROLE: str
    _learner: Any





    def _non_actionable_reason(self, action: dict[str, Any]) -> str:
        """Return the reconciliation reason, or ``''`` when still actionable."""
        detail = action.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        classified = detail.get("classified")
        classified = classified if isinstance(classified, dict) else {}
        diagnosis = classified.get("diagnosis")
        diagnosis = diagnosis if isinstance(diagnosis, dict) else {}
        error_type = str(
            classified.get("error_type") or diagnosis.get("error_type") or ""
        )
        target = str(
            classified.get("target_file") or diagnosis.get("file") or ""
        )
        method = str(
            action.get("proposed_method") or diagnosis.get("action") or ""
        )
        risk = str(action.get("risk") or "")
        if self._action_expired(action):
            return "expired-unconfirmed"
        if method == "fallback" and risk == "unclassified" and not error_type and not target:
            return "unclassifiable-fallback"
        return ""

    def _learn_non_actionable_fault(
        self, action: dict[str, Any], reason: str
    ) -> dict[str, Any]:
        from tasks.repair_learning import (
            ErrorSignature,
            RepairOutcome,
            _normalize_error_signature,
        )
        from uuid import uuid4

        detail = action.get("detail")
        detail = detail if isinstance(detail, dict) else {}
        error_class = str(
            action.get("scope") or detail.get("failure_code") or "UNKNOWN_FAULT"
        )
        message = str(action.get("summary") or error_class)
        signature_hash = _normalize_error_signature(error_class, message)
        signature = ErrorSignature(
            signature_hash=signature_hash,
            error_class=error_class,
            message_pattern=message[:200],
            failure_code=str(detail.get("failure_code") or error_class),
            file_context="",
            target_tool_id="main-system",
        )
        outcome = RepairOutcome(
            run_id=uuid4().hex,
            signature_hash=signature_hash,
            remedy=NON_ACTIONABLE_REMEDY,
            ok=False,
            detail={
                "reason": reason,
                "action_id": str(action.get("action_id") or ""),
                "evidence_digest": str(action.get("evidence_digest") or ""),
            },
        )
        promotion = self._learner.learn_from_outcome(signature, outcome)
        return {
            "action_id": str(action.get("action_id") or ""),
            "signature_hash": signature_hash,
            "reason": reason,
            "promotion": promotion,
        }

    def _audit_reconciliation(
        self,
        root: Path,
        removed: list[str],
        reasons: dict[str, str],
        learned: list[dict[str, Any]],
        before: int,
        after: int,
        *,
        marked: list[str] | None = None,
        marked_reasons: dict[str, str] | None = None,
        reconciled_requests: list[str] | None = None,
    ) -> None:
        try:
            path = root.joinpath(*RECONCILIATION_AUDIT_RELATIVE)
            path.parent.mkdir(parents=True, exist_ok=True)
            entry = {
                "timestamp": self._iso_now(),
                "actor": self.ROLE,
                "event": "fault-messages-reconciled",
                "removed": removed,
                "reasons": reasons,
                "marked": list(marked or []),
                "marked_reasons": dict(marked_reasons or {}),
                "reconciled_requests": list(reconciled_requests or []),
                "learned_signatures": [
                    {
                        "action_id": item["action_id"],
                        "signature_hash": item["signature_hash"],
                    }
                    for item in learned
                ],
                "pending_before": before,
                "pending_after": after,
            }
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n"
                )
        except OSError:
            pass

    def _reconcile_repair_requests(
        self,
        root: Path,
        pending_actions: list[dict[str, Any]],
        absorbed_ids: set[str],
    ) -> list[str]:
        """Retire repair requests that can no longer be confirmed (A154).

        A request whose pending action was absorbed by reconciliation — or
        whose pending action no longer exists at all (the queue item was
        removed after the request was created) — can never be confirmed.
        It must stop presenting as a live fault, but its record stays in
        the information layer as evidence and is only annotated in place.

        Requests younger than the grace window are left alone so a
        just-created request whose pending action is still being recorded
        is never retired by mistake.
        """
        try:
            from tasks.repair_coordinator import RepairCoordinator
        except Exception:
            return []
        coordinator = RepairCoordinator(Path(root))
        awaiting = {
            str(request.get("request_id") or ""): request
            for request in coordinator.awaiting_confirmation_requests()
        }
        if not awaiting:
            return []
        existing_action_ids = {
            str(action.get("action_id") or "") for action in pending_actions
        }
        reconciled: list[str] = []
        for request_id, request in awaiting.items():
            linked_action_id = f"repair-{request_id}"
            linked_absorbed = linked_action_id in absorbed_ids
            orphaned = linked_action_id not in existing_action_ids
            if not linked_absorbed and not orphaned:
                continue
            if orphaned and not linked_absorbed and not self._request_past_grace(request):
                continue
            coordinator.mark_request_status(
                request_id,
                "reconciled-non-actionable",
                reconciled_at=self._iso_now(),
                reconciled_by=self.ROLE,
                reconciled_reason=(
                    "pending-action-absorbed"
                    if linked_absorbed
                    else "pending-action-missing"
                ),
            )
            reconciled.append(request_id)
        return reconciled

    def _request_past_grace(self, request: dict[str, Any]) -> bool:
        raw = str(request.get("awaiting_confirmation_at") or "").strip()
        if not raw:
            return True
        try:
            confirmed_at = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return True
        if confirmed_at.tzinfo is None:
            confirmed_at = confirmed_at.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - confirmed_at
        return age.total_seconds() > _ORPHANED_REQUEST_GRACE_SECONDS

    def reconcile_pending_fault_messages(
        self, project_root: str | Path | None = None
    ) -> dict[str, Any]:
        """學習系統消除訊息：吸收非可行動證據後移除星澄待確認訊息。

        Three bounded reconciliation paths:

        * ``awaiting-confirmation`` items classified as non-actionable are
          absorbed into the learning store and removed from the queue
          (existing behaviour);
        * already-terminal items (``expired``) are absorbed and annotated
          in place as reconciled — terminal evidence is never deleted,
          it only stops being presented as a live fault; and
        * linked/orphaned repair requests are annotated as
          ``reconciled-non-actionable`` so a request that can no longer be
          confirmed does not stay on the fault surface.
        """
        self._ensure_learner()
        root = Path(project_root).resolve() if project_root else self._project_root()
        if self._learner is None:
            return {
                "ok": False,
                "reason": "learning-store-unavailable",
                "removed": [],
                "marked": [],
                "reconciled_requests": [],
            }
        from core_system.auto_action_policy import (
            TERMINAL_PENDING_STATUSES,
            mark_pending_actions_reconciled,
            read_pending_actions,
            remove_pending_actions,
        )

        actions = read_pending_actions(root)
        learned: list[dict[str, Any]] = []
        reasons: dict[str, str] = {}
        terminal_reasons: dict[str, str] = {}
        for action in actions:
            action_id = str(action.get("action_id") or "")
            if not action_id:
                continue
            status = str(action.get("status") or "")
            if status == "awaiting-confirmation":
                reason = self._non_actionable_reason(action)
                if not reason:
                    continue
                learned.append(self._learn_non_actionable_fault(action, reason))
                reasons[action_id] = reason
                continue
            if status in TERMINAL_PENDING_STATUSES and not action.get("reconciliation"):
                reason = "expired-unconfirmed"
                learned.append(self._learn_non_actionable_fault(action, reason))
                terminal_reasons[action_id] = reason
        removed = (
            remove_pending_actions(
                root,
                reasons.keys(),
                actor=self.ROLE,
                reason="learned-non-actionable-fault",
            )
            if reasons
            else []
        )
        marked = (
            mark_pending_actions_reconciled(
                root,
                terminal_reasons.keys(),
                actor=self.ROLE,
                reason="terminal-fault-evidence",
            )
            if terminal_reasons
            else []
        )
        absorbed_ids = {
            str(action.get("action_id") or "")
            for action in actions
            if str(action.get("action_id") or "") in reasons
            or str(action.get("action_id") or "") in terminal_reasons
        }
        reconciled_requests = self._reconcile_repair_requests(
            root, read_pending_actions(root), absorbed_ids
        )
        remaining = len(read_pending_actions(root))
        if removed or marked or reconciled_requests:
            self._audit_reconciliation(
                root,
                removed,
                reasons,
                learned,
                len(actions),
                remaining,
                marked=marked,
                marked_reasons=terminal_reasons,
                reconciled_requests=reconciled_requests,
            )
        return {
            "ok": True,
            "role": self.ROLE,
            "removed": removed,
            "marked": marked,
            "reconciled_requests": reconciled_requests,
            "learned": learned,
            "remaining": remaining,
        }


__all__ = ["LearningReconciliationMixin"]
