"""Learning Evidence Sync Sub-Sovereign — 學習證據同步子主權（子屬同步主宰，無決策、無執行）。

法典依據:
- sovereign_id: learning-evidence-sync-sub-sovereign (position 32)
- area: system-learning
- rank: child-of-synchronization-sovereign-no-decision-no-execution
- basis: A310/A322 (retires learning-system-sovereign /
  learning-system-sub-sovereign)

The implementation was merged from the retired
``core_system.learning_system_sovereign.LearningSystemSovereign`` so the
active path keeps its persistent system-error learning behavior (fault
learning, repair outcomes, evidence feedback) while operating under the
codex ``learning-evidence-sync-sub-sovereign`` identity.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from ._base import SubSovereignBase
from governance_rule.execution.codex_official import official_self_declaration


_DECLARATION = official_self_declaration("learning-evidence-sync-sub-sovereign")
if _DECLARATION is None:
    raise RuntimeError("learning evidence sync sub-sovereign not found in Governance Codex")

RECONCILIATION_AUDIT_RELATIVE: Final[tuple[str, ...]] = (
    "main-system",
    "runtime",
    "state",
    "learning-fault-reconciliation.jsonl",
)
NON_ACTIONABLE_REMEDY: Final[str] = "no-action-required"


class LearningEvidenceSyncSubSovereign(SubSovereignBase):
    """Learns verified error/remedy outcomes without gaining execution power."""

    sovereign_id = "learning-evidence-sync-sub-sovereign"
    parent_sovereign_id = "synchronization-sovereign"

    ROLE = sovereign_id

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._store: Any | None = None
        self._learner: Any | None = None
        self._sync_state: dict[str, Any] = {}

    async def start(self) -> dict[str, Any]:
        from tasks.repair_learning import RepairLearner, RepairLearningStore

        root = Path(getattr(self.app, "project_root", ".")).resolve()
        self._store = RepairLearningStore(root / "main-system" / "data" / "automatic-repair")
        self._learner = RepairLearner(self._store)
        self._started = True
        # E173: activation returns a light receipt — analyze_history()
        # runs on demand in status(), not on the startup critical path.
        return {
            "ok": True,
            "role": self.ROLE,
            "started": self._started,
            "duties": list(_DECLARATION.duties),
            "execution": "governed-executor-only",
            "persistence": "repair-learning-sqlite",
        }

    async def stop(self) -> None:
        self._started = False

    def sync_learning_evidence(self, evidence: dict[str, Any]) -> None:
        """同步學習證據。"""
        self._sync_state = {
            "learning_evidence": evidence,
            "synced_at": self._iso_now(),
        }

    def status(self) -> dict[str, Any]:
        analysis = self._learner.analyze_history() if self._learner is not None else {}
        return {
            "role": self.ROLE,
            "started": self._started,
            "parent": self.parent_sovereign_id,
            "duties": list(_DECLARATION.duties),
            "execution": "governed-executor-only",
            "persistence": "repair-learning-sqlite",
            "analysis": analysis,
        }

    def live_status(self) -> dict[str, Any]:
        base = self.status()
        base["sync_state"] = self._sync_state
        return base

    def learn_outcome(self, signature: Any, outcome: Any) -> dict[str, Any]:
        if self._learner is None:
            return {"recorded": False, "reason": "learning-sovereign-not-ready"}
        return self._learner.learn_from_outcome(signature, outcome)

    def suggest_remedy(self, signature: Any) -> dict[str, Any]:
        if self._learner is None:
            return {"suggested": False, "reason": "learning-sovereign-not-ready"}
        return self._learner.suggest_remedy(signature)

    # ------------------------------------------------------------------
    # Learning-driven fault-message reconciliation
    # ------------------------------------------------------------------

    def _project_root(self) -> Path:
        raw = getattr(self.app, "project_root", None)
        if raw:
            return Path(raw).resolve()
        # governance/sub-sovereigns/... -> main-system -> GPTBridge
        return Path(__file__).resolve().parents[3]

    def _ensure_learner(self) -> None:
        if self._learner is not None:
            return
        try:
            from tasks.repair_learning import RepairLearner, RepairLearningStore

            root = self._project_root()
            self._store = RepairLearningStore(
                root / "main-system" / "data" / "automatic-repair"
            )
            self._learner = RepairLearner(self._store)
        except Exception:
            self._store = None
            self._learner = None

    @staticmethod
    def _action_expired(action: dict[str, Any]) -> bool:
        raw = str(action.get("expires_at") or "").strip()
        if not raw:
            return False
        try:
            expiry = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return True
        return datetime.now(timezone.utc) > expiry

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
        """學習系統消除訊息：吸收非可行動證據後移除星澄待確認訊息。

        Codex duties (A310/A322, learning-evidence-sync-sub-sovereign):
        INPUT typed-errors + repair outcomes; LEARN normalized signature;
        EVIDENCE feedback.  Only items already reconciled as non-actionable
        (expired-unconfirmed or unclassifiable fallback) are absorbed into
        the persistent learning store and then removed from the user-facing
        pending queue so 星澄 no longer displays them.  Actionable or
        confirmed items are never touched; each removal is audited with
        actor/reason/evidence without secrets.
        """
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


__all__ = ["LearningEvidenceSyncSubSovereign"]
