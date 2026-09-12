"""Maintenance Sovereign — lifecycle mixin (start / stop).

Extracted from ``maintenance_sovereign`` (retired, A302/A323) to keep each module focused and
under 500 lines.  Preserves the start() ordering that reloads learning state
and the stop() behavior that preserves learning state.

Per A152/A154 (amended codex), the maintenance sovereign owns system health
only; the health-classification loop (was the A67/A72 repair decision loop)
is started here but the repair decision is delegated to the
decision-sovereign.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .sovereign_utils import _iso_now, _suppress

from .maintenance_learning import MaintenanceLearningMixin


class MaintenanceLifecycleMixin(MaintenanceLearningMixin):
    """Lifecycle methods for the Maintenance Sovereign.

    Expects the following attributes to be set by the composing class's
    ``__init__``:

      * ``self.app`` — the GPTBridgeApp instance
      * ``self._started`` — bool
      * ``self._started_at`` / ``self._stopped_at`` — ISO timestamps or None
      * ``self._daily_cleaner`` / ``self._hot_update`` / ``self._repair_service``
      * ``self._health_checker``
      * ``self._capability_task`` / ``self._capability_interval_seconds``
      * ``self._learning_store`` / ``self._learner`` / ``self._learning_analysis``
      * ``self.ROLE``
    """

    async def start(
        self,
        *,
        daily_cleaner: Any = None,
        hot_update: Any = None,
        repair_service: Any = None,
        health_checker: Any = None,
        capability_interval_seconds: float = 3600.0,
    ) -> dict[str, Any]:
        """Start the Maintenance Sovereign and its in-process maintenance loops.

        ``daily_cleaner``: the DailyGlobalCleanerService instance already built
            and started by the app (its loop runs independently).
        ``hot_update``: the governed HotUpdateService; the sovereign supervises
            the frozen, version-gated update boundary (decision only).
        ``repair_service``: the governed CentralRepairService; the sovereign
            surfaces fault-determination, automatic-repair and backup-extraction
            status but never runs the heavy work in-process.
        ``health_checker``: a callable returning a health report dict (default
            ``core.health.check_core_health``), used for system-health monitoring.
        ``capability_interval_seconds``: interval for the independent
            capability/installation check loop (default hourly).  The check is
            read-only detection only; it never installs anything.
        """

        self._daily_cleaner = daily_cleaner
        self._hot_update = hot_update
        self._repair_service = repair_service
        if health_checker is not None:
            self._health_checker = health_checker
        self._capability_interval_seconds = max(
            300.0, float(capability_interval_seconds)
        )
        self._started_at = _iso_now()
        self._started = True

        # Reload persistent learning state so the sovereign's knowledge is
        # not reset by an auto-repair restart.  The SQLite store survives
        # backend crashes; the in-memory sovereign is recreated but rehydrates
        # from the persisted error signatures and learned recipes.
        self._ensure_learning_store()
        if self._learner is not None:
            try:
                self._learning_analysis = await asyncio.to_thread(
                    self._learner.analyze_history
                )
            except Exception:
                self._learning_analysis = None

        if self._capability_task is None:
            self._capability_task = asyncio.create_task(
                self._capability_check_loop(),
                name="health-maintenance-test-sub-sovereign-capability",
            )

        # A152/A154: start the health-classification loop so the maintenance
        # sovereign classifies pending health signals and delegates the
        # repair decision to the decision-sovereign.
        self._start_repair_decision_loop()

        return {
            "ok": True,
            "role": self.ROLE,
            "started_at": self._started_at,
            "responsibilities": list(self._maintenance_responsibilities()),
            "daily_cleaner": self._daily_cleaner_status(),
        }

    async def stop(self) -> None:
        if self._capability_task is not None:
            self._capability_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._capability_task
            self._capability_task = None
        # A152/A154: stop the health-classification loop.
        await self._stop_repair_decision_loop()
        self._capability_report = None
        self._daily_cleaner = None
        self._hot_update = None
        self._repair_service = None
        # NOTE: learning state (_learning_store, _learner, _learning_analysis)
        # is intentionally NOT cleared.  The SQLite-backed store survives
        # auto-repair restarts; clearing it here would lose the accumulated
        # error signatures and learned recipes that the sovereign rehydrates
        # from on the next start().
        self._started = False
        self._stopped_at = _iso_now()

    def _stop_requested(self) -> bool:
        return not self._started

    def _maintenance_responsibilities(self):
        """Return the maintenance responsibilities list.

        Provided as a method so mixins don't need to import the module-level
        constant directly (avoids circular imports).
        """
        from governance.sub_sovereigns.health_maintenance_test_sub_sovereign import MAINTENANCE_RESPONSIBILITIES
        return MAINTENANCE_RESPONSIBILITIES
