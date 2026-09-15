"""Learning Sub-Sovereign — 學習子主宰（星澄專屬管理，無決策、無執行）。

法典依據 (A485 — learning-sub-sovereign-transfer-to-xingcheng):
- sovereign_id: learning-evidence-sync-sub-sovereign (identity preserved)
- display name: learning-sub-sovereign
- area: system-learning
- parent: 星澄 (xingcheng_sovereign)
- relation: privileged-institution-managed-sub-sovereign
- rank: child-of-星澄-no-decision-no-execution
- duties: manage learning-evidence modules, assign bounded learning work,
  coordinate evidence normalization/evaluation/retention, collect outcome proof
- boundary: no decision/execution power; cannot alter models, code, Codex,
  permissions, routing or active behavior
- information: 星澄-to-learning instructions/events/evidence/results use the
  information layer (including the 星澄 auxiliary private channel)

Module home: ``governance/sovereigns/xingcheng/`` (A485 module assignment to
the 星澄 owner).  The implementation was merged from the retired
``core_system.learning_system_sovereign.LearningSystemSovereign`` so the
active path keeps its persistent system-error learning behavior (fault
learning, repair outcomes, evidence feedback) while operating under the
codex ``learning-evidence-sync-sub-sovereign`` identity.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from governance.sub_sovereigns._base import SubSovereignBase
from governance_rule.execution.codex_official import official_self_declaration
from .learning_reconciliation import LearningReconciliationMixin


_logger = logging.getLogger("gptbridge.sovereign.learning-evidence-sync")

_DECLARATION = official_self_declaration("learning-evidence-sync-sub-sovereign")
if _DECLARATION is None:
    raise RuntimeError("learning evidence sync sub-sovereign not found in Governance Codex")

from .learning_constants import (
    RECONCILIATION_AUDIT_RELATIVE,
    NON_ACTIONABLE_REMEDY,
    DEFAULT_RECONCILE_INTERVAL_SECONDS,
    RECONCILE_EVENT_POLL_SECONDS,
    RECONCILE_INTERVAL_ENV,
)


class LearningEvidenceSyncSubSovereign(SubSovereignBase, LearningReconciliationMixin):
    """Learns verified error/remedy outcomes without gaining execution power."""

    sovereign_id = "learning-evidence-sync-sub-sovereign"
    parent_sovereign_id = "星澄"

    ROLE = sovereign_id

    def __init__(
        self,
        app: Any | None = None,
        parent: Any | None = None,
        *,
        reconcile_interval: float | None = None,
    ) -> None:
        super().__init__(app, parent)
        self._store: Any | None = None
        self._learner: Any | None = None
        self._sync_state: dict[str, Any] = {}
        self._reconcile_task: asyncio.Task[Any] | None = None
        self._reconcile_interval = reconcile_interval
        self._last_reconciliation: dict[str, Any] = {}

    async def start(self) -> dict[str, Any]:
        from tasks.repair_learning import RepairLearner, RepairLearningStore

        root = Path(getattr(self.app, "project_root", ".")).resolve()
        self._store = RepairLearningStore(root / "main-system" / "data" / "automatic-repair")
        self._learner = RepairLearner(self._store)
        self._started = True
        self._start_reconcile_loop()
        # E173: activation returns a light receipt — analyze_history()
        # runs on demand in status(), not on the startup critical path.
        return {
            "ok": True,
            "role": self.ROLE,
            "started": self._started,
            "duties": list(_DECLARATION.duties),
            "execution": "governed-executor-only",
            "persistence": "repair-learning-sqlite",
            "reconciliation": "automatic",
        }

    async def stop(self) -> None:
        await self._stop_reconcile_loop()
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
            "reconciliation": dict(self._last_reconciliation),
            "reconcile_loop": bool(
                self._reconcile_task is not None and not self._reconcile_task.done()
            ),
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

    def _interval_seconds(self) -> float:
        raw: Any = self._reconcile_interval
        if raw is None:
            raw = os.environ.get(RECONCILE_INTERVAL_ENV, "")
        try:
            value = (
                float(raw)
                if str(raw).strip()
                else DEFAULT_RECONCILE_INTERVAL_SECONDS
            )
        except (TypeError, ValueError):
            value = DEFAULT_RECONCILE_INTERVAL_SECONDS
        return max(0.0, float(value))

    def _start_reconcile_loop(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._reconcile_task is not None and not self._reconcile_task.done():
            return
        self._reconcile_task = loop.create_task(self._reconcile_loop())

    async def _stop_reconcile_loop(self) -> None:
        task = self._reconcile_task
        self._reconcile_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            pass

    async def reconcile_once(self) -> dict[str, Any]:
        """Run one reconciliation pass and record its receipt."""
        receipt = await asyncio.to_thread(self.reconcile_pending_fault_messages)
        self._last_reconciliation = {
            "at": self._iso_now(),
            "ok": bool(receipt.get("ok")),
            "removed": len(receipt.get("removed") or []),
            "learned": len(receipt.get("learned") or []),
            "remaining": receipt.get("remaining"),
        }
        if self._last_reconciliation["removed"]:
            _logger.info(
                "learning reconciliation removed %s pending message(s)",
                self._last_reconciliation["removed"],
            )
        return receipt

    async def _wait_for_cycle(self) -> None:
        """Wait one interval, waking early when the fault surface changes."""
        event = None
        try:
            from core_system.auto_action_policy import fault_change_event

            event = fault_change_event()
        except Exception:
            event = None
        remaining = self._interval_seconds()
        while True:
            if event is not None and event.is_set():
                event.clear()
                return
            if remaining <= 0:
                await asyncio.sleep(0.01)
                return
            nap = min(remaining, RECONCILE_EVENT_POLL_SECONDS)
            await asyncio.sleep(nap)
            remaining -= nap
            if remaining <= 0:
                return

    async def _reconcile_loop(self) -> None:
        """Boot pass + periodic passes; fault-change events wake it early."""
        while True:
            try:
                if self._started:
                    await self.reconcile_once()
            except asyncio.CancelledError:
                raise
            except Exception as error:  # automation must never die
                _logger.warning("learning reconciliation pass failed: %s", error)
            await self._wait_for_cycle()


__all__ = ["LearningEvidenceSyncSubSovereign"]
