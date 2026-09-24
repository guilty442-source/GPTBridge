"""Xingcheng Sovereign — Learning Command Mixin (A485).

星澄驅動其內建學習能力：auto-learning is armed, driven and disarmed
in-process by the owning sovereign (A592/A604 — no module/child layer;
the capability is part of 星澄 itself).  The engine never self-arms;
every learning action is a bounded command issued by the owner.
"""

from __future__ import annotations

import logging
from typing import Any

from .learning_engine import XingchengLearningEngine

_logger = logging.getLogger("gptbridge.sovereign.xingcheng.learning-command")

_COMMAND_LOG_LIMIT = 50

# Learning pass triggers
_LEARNING_TRIGGERS = frozenset({"auto-loop", "anomaly", "failure", "user-request", "scheduled"})

# Maximum learning pass history
_LEARNING_HISTORY_LIMIT = 100


class XingchengLearningCommandMixin:
    """Owner command surface for the internal learning engine (A485)."""

    app: Any
    sovereign_id: str
    _learning_engine: XingchengLearningEngine
    _learning_commands: list[dict[str, Any]]
    _learning_armed: bool
    _learning_history: list[dict[str, Any]]
    _last_reconciliation: dict[str, Any]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._learning_engine = XingchengLearningEngine(self.app)
        self._learning_commands = []
        self._learning_armed = False
        self._learning_history = []
        self._last_reconciliation = {}

    async def _command_learning(
        self, intent: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Issue one bounded learning command to the internal engine."""
        if not self._learning_engine.started:
            return {
                "commanded": False,
                "reason": "learning-engine-not-started",
                "intent": intent,
            }
        outcome = await self._learning_engine.run_command(intent, payload)
        receipt = {
            "at": self._iso_now(),
            "intent": intent,
            "accepted": bool(outcome.accepted),
            "reason": outcome.refusal.reason_code if outcome.refusal else "",
        }
        self._learning_commands.append(receipt)
        del self._learning_commands[:-_COMMAND_LOG_LIMIT]

        # Record in history
        history_entry = {
            "at": receipt["at"],
            "intent": intent,
            "trigger": payload.get("trigger") if payload else None,
            "accepted": receipt["accepted"],
            "reason": receipt["reason"],
        }
        self._learning_history.append(history_entry)
        if len(self._learning_history) > _LEARNING_HISTORY_LIMIT:
            self._learning_history = self._learning_history[-_LEARNING_HISTORY_LIMIT:]

        if not outcome.accepted:
            _logger.info(
                "learning command %s refused: %s", intent, receipt["reason"]
            )
        return {
            "commanded": bool(outcome.accepted),
            "receipt": receipt,
            "result": outcome.result or {},
        }

    async def start_learning_automation(self) -> dict[str, Any]:
        """Arm the engine's auto-learning loop (commanded, never self-armed)."""
        result = await self._command_learning("learn.auto-start")
        self._learning_armed = bool(result.get("commanded"))
        return result

    async def ensure_learning_automation(self) -> bool:
        """Re-command learning until the engine accepts it (A485).

        ``start_supervision`` can run before the engine is started, so the
        first ``learn.auto-start`` can fail closed
        (``learning-engine-not-started``) and nothing retried it — leaving
        commanded learning (and its fault-message reconciliation) off for
        the whole process generation.  The auto-loop calls this every
        cycle until the command is accepted.
        """
        if self._learning_armed:
            return True
        result = await self.start_learning_automation()
        return bool(result.get("commanded"))

    async def stop_learning_automation(self) -> dict[str, Any]:
        """Disarm the engine's auto-learning loop."""
        result = await self._command_learning("learn.auto-stop")
        self._learning_armed = False
        return result

    async def command_learning_pass(
        self, trigger: str = "auto-loop"
    ) -> dict[str, Any]:
        """Command one bounded reconciliation/learning pass on the engine."""
        if trigger not in _LEARNING_TRIGGERS:
            trigger = "auto-loop"
        return await self._command_learning(
            "learn.reconcile", {"trigger": trigger}
        )

    async def push_learning_outcome(
        self, signature: dict[str, Any], outcome: dict[str, Any]
    ) -> dict[str, Any]:
        """Push one verified outcome for the engine to learn from."""
        return await self._command_learning(
            "learn.outcome",
            {"signature": dict(signature), "outcome": dict(outcome)},
        )

    async def command_learning_analysis(self) -> dict[str, Any]:
        """Request the engine's learning-history analysis."""
        return await self._command_learning("learn.analyze")

    async def command_learning_retry_failed(
        self, failed_signatures: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Command retry of failed learning items."""
        return await self._command_learning(
            "learn.retry", {"failed": failed_signatures}
        )

    async def teach_repair_knowledge(
        self,
        *,
        signature: dict[str, Any],
        remedy: str,
        name: str = "",
        verification: str = "",
        automatic: bool = True,
    ) -> dict[str, Any]:
        """Teach the engine one bounded repair doctrine entry (learn.teach).

        Doctrine is stored as a ``source="taught"`` recipe — distinct
        from outcome-earned (``learned``) knowledge — and forwarded to
        the model's governed teaching gate so repair knowledge settles
        into model capability as well as the evidence store.
        """
        return await self._command_learning(
            "learn.teach",
            {
                "signature": dict(signature),
                "remedy": str(remedy),
                "name": str(name),
                "verification": str(verification),
                "automatic": bool(automatic),
            },
        )

    def learning_status(self) -> dict[str, Any]:
        """Read-only projection of the internal learning capability."""
        engine = self._learning_engine
        if not engine.started:
            return {
                "materialized": True,
                "capability": "learning-evidence-sync",
                "engine_started": False,
                "armed": self._learning_armed,
                "commands_issued": len(self._learning_commands),
            }
        engine_status = engine.status()
        return {
            "materialized": True,
            "capability": "learning-evidence-sync",
            "armed": self._learning_armed,
            "engine_started": engine.started,
            "auto_learning": engine_status.get("auto_learning", "disarmed"),
            "reconcile_loop": engine_status.get("reconcile_loop", False),
            "commands_issued": len(self._learning_commands),
            "last_command": (
                self._learning_commands[-1] if self._learning_commands else None
            ),
            "last_reconciliation": self._last_reconciliation,
            "history": self._learning_history[-10:],
        }
