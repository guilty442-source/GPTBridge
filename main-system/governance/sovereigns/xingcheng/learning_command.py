"""Xingcheng Sovereign — Learning Command Mixin (A485).

星澄指揮學習子主宰自動學習：auto-learning is armed, driven and disarmed
through the governed delegation path (single-use nonce + verifiable
receipts, A334/A435).  The child never self-arms; every learning action
is a bounded assignment issued by the codex-registered parent.
"""

from __future__ import annotations

import logging
from typing import Any

from core_system.codex_decision import SovereignRequest

_logger = logging.getLogger("gptbridge.sovereign.xingcheng.learning-command")

_LEARNING_CHILD_ID = "learning-evidence-sync-sub-sovereign"
_COMMAND_LOG_LIMIT = 50


class XingchengLearningCommandMixin:
    """Parent-command surface for the learning sub-sovereign (A485)."""

    _sub_sovereigns: dict[str, Any]
    app: Any
    sovereign_id: str
    _learning_commands: list[dict[str, Any]]
    _learning_armed: bool

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._learning_commands = []
        self._learning_armed = False

    async def _command_learning(
        self, intent: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Delegate one bounded learning command to the codex child."""
        from ...registries import validate_child_parent

        if not validate_child_parent(_LEARNING_CHILD_ID, self.sovereign_id):
            return {
                "commanded": False,
                "reason": "child-parent-mismatch",
                "intent": intent,
            }
        outcome = await self.delegate_to(
            _LEARNING_CHILD_ID,
            SovereignRequest(
                intent=intent,
                subject="learning",
                requester=self.sovereign_id,
                payload=dict(payload or {}),
            ),
        )
        receipt = {
            "at": self._iso_now(),
            "intent": intent,
            "accepted": bool(outcome.accepted),
            "reason": outcome.refusal.reason_code if outcome.refusal else "",
        }
        self._learning_commands.append(receipt)
        del self._learning_commands[:-_COMMAND_LOG_LIMIT]
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
        """Arm the child's auto-learning loop (commanded, never self-armed)."""
        result = await self._command_learning("learn.auto-start")
        self._learning_armed = bool(result.get("commanded"))
        return result

    async def stop_learning_automation(self) -> dict[str, Any]:
        """Disarm the child's auto-learning loop."""
        result = await self._command_learning("learn.auto-stop")
        self._learning_armed = False
        return result

    async def command_learning_pass(
        self, trigger: str = "auto-loop"
    ) -> dict[str, Any]:
        """Command one bounded reconciliation/learning pass."""
        return await self._command_learning(
            "learn.reconcile", {"trigger": trigger}
        )

    async def push_learning_outcome(
        self, signature: dict[str, Any], outcome: dict[str, Any]
    ) -> dict[str, Any]:
        """Push one verified outcome for the child to learn from."""
        return await self._command_learning(
            "learn.outcome",
            {"signature": dict(signature), "outcome": dict(outcome)},
        )

    async def command_learning_analysis(self) -> dict[str, Any]:
        """Request the child's learning-history analysis."""
        return await self._command_learning("learn.analyze")

    def learning_status(self) -> dict[str, Any]:
        """Read-only projection of the commanded learning surface."""
        child = self._sub_sovereigns.get(_LEARNING_CHILD_ID)
        if child is None:
            return {
                "materialized": False,
                "child": _LEARNING_CHILD_ID,
                "armed": self._learning_armed,
                "commands_issued": len(self._learning_commands),
            }
        reporter = getattr(child, "status", None)
        child_status = reporter() if callable(reporter) else {}
        return {
            "materialized": True,
            "child": _LEARNING_CHILD_ID,
            "armed": self._learning_armed,
            "child_started": bool(getattr(child, "_started", False)),
            "auto_learning": child_status.get("auto_learning", "disarmed"),
            "reconcile_loop": child_status.get("reconcile_loop", False),
            "commands_issued": len(self._learning_commands),
            "last_command": (
                self._learning_commands[-1] if self._learning_commands else None
            ),
            "last_reconciliation": child_status.get("reconciliation", {}),
        }


__all__ = ["XingchengLearningCommandMixin"]
