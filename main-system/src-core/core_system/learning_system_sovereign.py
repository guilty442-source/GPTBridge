"""Decision owner for persistent system-error learning."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from governance_rule.codex import GOVERNANCE_CODEX


_DECLARATION = next(
    (item for item in GOVERNANCE_CODEX.sovereigns if item.area == "system-learning"),
    None,
)


class LearningSystemSovereign:
    """Learns verified error/remedy outcomes without gaining execution power."""

    ROLE = "learning-system-sovereign"

    def __init__(self, app: Any) -> None:
        if _DECLARATION is None:
            raise RuntimeError("learning system sovereign not found in Governance Codex")
        self.app = app
        self._store: Any | None = None
        self._learner: Any | None = None
        self._started = False

    async def start(self) -> dict[str, Any]:
        from tasks.repair_learning import RepairLearner, RepairLearningStore

        root = Path(getattr(self.app, "project_root", ".")).resolve()
        self._store = RepairLearningStore(root / "main-system" / "data" / "automatic-repair")
        self._learner = RepairLearner(self._store)
        self._started = True
        return self.status()

    async def stop(self) -> None:
        self._started = False

    def status(self) -> dict[str, Any]:
        analysis = self._learner.analyze_history() if self._learner is not None else {}
        return {
            "role": self.ROLE,
            "started": self._started,
            "duties": list(_DECLARATION.duties),
            "execution": "governed-executor-only",
            "persistence": "repair-learning-sqlite",
            "analysis": analysis,
        }

    def learn_outcome(self, signature: Any, outcome: Any) -> dict[str, Any]:
        if self._learner is None:
            return {"recorded": False, "reason": "learning-sovereign-not-ready"}
        return self._learner.learn_from_outcome(signature, outcome)

    def suggest_remedy(self, signature: Any) -> dict[str, Any]:
        if self._learner is None:
            return {"suggested": False, "reason": "learning-sovereign-not-ready"}
        return self._learner.suggest_remedy(signature)


__all__ = ["LearningSystemSovereign"]
