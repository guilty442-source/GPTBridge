"""Learning reconciliation mixin (A185 split).

Contains the fault reconciliation methods extracted from
LearningEvidenceSyncSubSovereign.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .learning_evidence_sync_constants import (
    RECONCILIATION_AUDIT_RELATIVE,
    NON_ACTIONABLE_REMEDY,
)


class LearningReconciliationMixin:
    """Fault message reconciliation and learning."""

    ROLE: str
    _learner: Any

    def _iso_now(self) -> str:
        raise NotImplementedError

    def _project_root(self) -> Path:
        raise NotImplementedError

    def _ensure_learner(self) -> None:
        raise NotImplementedError

    def _action_expired(self, action: dict[str, Any]) -> bool:
        raise NotImplementedError

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

    def reconcile_pending_fault_messages(
        self, project_root: str | Path | None = None
    ) -> dict[str, Any]:
        """學習系統消除訊息：吸收非可行動證據後移除星澄待確認訊息。"""
        self._ensure_learner()
        root = Path(project_root).resolve() if project_root else self._project_root()
        if self._learner is None:
            return {
                "ok": False,
                "reason": "learning-store-unavailable",
                "removed": [],
            }
        from core_system.auto_action_policy import (
            read_pending_actions,
            remove_pending_actions,
        )

        actions = read_pending_actions(root)
        learned: list[dict[str, Any]] = []
        reasons: dict[str, str] = {}
        for action in actions:
            if str(action.get("status") or "") != "awaiting-confirmation":
                continue
            reason = self._non_actionable_reason(action)
            action_id = str(action.get("action_id") or "")
            if not reason or not action_id:
                continue
            learned.append(self._learn_non_actionable_fault(action, reason))
            reasons[action_id] = reason
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
        remaining = len(read_pending_actions(root))
        if removed:
            self._audit_reconciliation(
                root, removed, reasons, learned, len(actions), remaining
            )
        return {
            "ok": True,
            "role": self.ROLE,
            "removed": removed,
            "learned": learned,
            "remaining": remaining,
        }


__all__ = ["LearningReconciliationMixin"]
