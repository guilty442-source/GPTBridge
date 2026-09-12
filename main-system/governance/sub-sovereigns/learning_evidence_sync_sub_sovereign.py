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

from pathlib import Path
from typing import Any

from ._base import SubSovereignBase
from governance_rule.codex import GOVERNANCE_CODEX


_DECLARATION = next(
    (item for item in GOVERNANCE_CODEX.sovereigns if item.area == "system-learning"),
    None,
)
if _DECLARATION is None:
    raise RuntimeError("learning evidence sync sub-sovereign not found in Governance Codex")


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


__all__ = ["LearningEvidenceSyncSubSovereign"]
