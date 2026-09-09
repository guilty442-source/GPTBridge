"""Maintenance Sovereign — persistent learning mixin.

Provides the learning-state methods that survive auto-repair restarts via a
SQLite-backed store.  Extracted from ``maintenance_sovereign`` to keep each
module focused and under 500 lines.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


class MaintenanceLearningMixin:
    """Persistent repair-learning state (survives auto-repair restarts).

    Expects the following attributes to be set by the composing class's
    ``__init__``:

      * ``self.app`` — the GPTBridgeApp instance
      * ``self._learning_store`` — persistent store (or ``None``)
      * ``self._learner`` — RepairLearner (or ``None``)
      * ``self._learning_analysis`` — cached analysis dict (or ``None``)
      * ``self.ROLE`` — the sovereign role identifier string
    """

    # ------------------------------------------------------------------
    # Persistent learning (survives auto-repair restarts)
    # ------------------------------------------------------------------

    def _ensure_learning_store(self) -> None:
        """Lazily attach to the persistent repair-learning SQLite store.

        The store lives under ``main-system/data/automatic-repair`` and is
        shared with ``CentralRepairService``.  By reading from the same
        SQLite database the sovereign's learning state survives backend
        restarts triggered by auto-repair — the in-memory sovereign is
        recreated, but the persisted error signatures, outcomes and learned
        recipes are reloaded on the next ``start()``.
        """
        if self._learning_store is not None:
            return
        try:
            project_root = Path(getattr(self.app, "project_root", ".") or ".")
            repair_data = project_root / "main-system" / "data" / "automatic-repair"
            from tasks.repair_learning import RepairLearner, RepairLearningStore

            self._learning_store = RepairLearningStore(repair_data)
            self._learner = RepairLearner(self._learning_store)
        except Exception:
            # Best-effort: learning is optional and never blocks maintenance.
            self._learning_store = None
            self._learner = None

    def learning_status(self) -> dict[str, Any]:
        """Surface the persistent learning state (not reset by auto-repair)."""
        self._ensure_learning_store()
        if self._learner is None:
            return {
                "enabled": False,
                "reason": "learning-store-unavailable",
                "authority": self.ROLE,
            }
        try:
            if self._learning_analysis is None:
                self._learning_analysis = self._learner.analyze_history()
            analysis = dict(self._learning_analysis)
            analysis["enabled"] = True
            analysis["authority"] = self.ROLE
            analysis["persistence"] = "sqlite-survives-restart"
            return analysis
        except Exception as error:
            return {
                "enabled": True,
                "authority": self.ROLE,
                "error": f"{type(error).__name__}: {error}",
                "persistence": "sqlite-survives-restart",
            }

    def record_repair_outcome(
        self,
        *,
        error_class: str,
        message: str,
        failure_code: str,
        remedy: str,
        ok: bool,
        file_path: str = "",
        target_tool_id: str = "main-system",
        run_id: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Record a repair outcome in the persistent learning store.

        The sovereign coordinates learning: when a governed executor (boot_core,
        CentralRepairService, ConnectionWatchdog) completes a repair, the
        sovereign records the outcome so recurring error→remedy patterns can
        be promoted to learned recipes.  This state is persisted in SQLite and
        is not reset by subsequent auto-repair restarts.
        """
        self._ensure_learning_store()
        if self._learner is None:
            return {"recorded": False, "reason": "learning-store-unavailable"}
        try:
            from tasks.repair_learning import ErrorSignature, RepairOutcome
            from uuid import uuid4

            from tasks.repair_learning import _normalize_error_signature

            signature_hash = _normalize_error_signature(
                error_class, message, file_path=file_path
            )
            signature = ErrorSignature(
                signature_hash=signature_hash,
                error_class=error_class,
                message_pattern=message[:200],
                failure_code=failure_code,
                file_context=file_path,
                target_tool_id=target_tool_id,
            )
            outcome = RepairOutcome(
                run_id=run_id or uuid4().hex,
                signature_hash=signature_hash,
                remedy=remedy,
                ok=ok,
                detail=detail or {},
            )
            promotion = self._learner.learn_from_outcome(signature, outcome)
            # Invalidate cached analysis so the next status call refreshes.
            self._learning_analysis = None
            return {
                "recorded": True,
                "signature_hash": signature_hash,
                "promotion": promotion,
                "persistence": "sqlite-survives-restart",
            }
        except Exception as error:
            return {
                "recorded": False,
                "error": f"{type(error).__name__}: {error}",
            }

    def suggest_remedy(
        self,
        *,
        error_class: str,
        message: str,
        failure_code: str = "",
        file_path: str = "",
    ) -> dict[str, Any]:
        """Query the learning store for the best known remedy for an error."""
        self._ensure_learning_store()
        if self._learner is None:
            return {"suggested": False, "reason": "learning-store-unavailable"}
        try:
            from tasks.repair_learning import ErrorSignature
            from tasks.repair_learning import _normalize_error_signature

            signature_hash = _normalize_error_signature(
                error_class, message, file_path=file_path
            )
            signature = ErrorSignature(
                signature_hash=signature_hash,
                error_class=error_class,
                message_pattern=message[:200],
                failure_code=failure_code,
                file_context=file_path,
            )
            return self._learner.suggest_remedy(signature)
        except Exception as error:
            return {
                "suggested": False,
                "error": f"{type(error).__name__}: {error}",
            }
