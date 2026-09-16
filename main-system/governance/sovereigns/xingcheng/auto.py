"""Xingcheng Sovereign — Auto-Automation Module (A20).

Background auto-loop: observes, analyzes, reasons, manages the owned domain.
All operations stay inside the owned domain; no system targets or effects.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .._base import SovereignBase, SovereignRequest, SovereignOutcome
from core_system.codex_decision import accepted_outcome, refusal_outcome

_logger = logging.getLogger("gptbridge.sovereign.xingcheng.auto")


class XingchengAutoMixin:
    """Full-automation upgrade — auto-loop inside owned domain."""

    _auto_loop_task: asyncio.Task[Any] | None
    _auto_loop_interval: float
    _auto_enabled: bool
    _auto_metrics: dict[str, Any]
    _last_snapshot: dict[str, Any]
    _pending_anomalies: list[dict[str, Any]]

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._auto_loop_task = None
        self._auto_loop_interval = 10.0
        self._auto_enabled = True
        self._auto_metrics = {
            "observe_cycles": 0, "analyze_cycles": 0, "reason_cycles": 0,
            "manage_cycles": 0, "health_checks": 0, "anomalies_detected": 0,
            "channel_notifications": 0, "db_maintenance_runs": 0,
            "model_loads": 0, "config_updates": 0, "learning_commands": 0,
            "last_auto_cycle": "", "last_anomaly": "",
        }
        self._last_snapshot = {}
        self._pending_anomalies = []

    async def _adjudicate_auto_observe(self, request: SovereignRequest) -> SovereignOutcome:
        snapshot = self._observe_domain()
        self._last_snapshot = snapshot
        return accepted_outcome(
            {"action": "auto-observe", "domain": "owned", "snapshot": snapshot},
            self.verified_basis("A20"),
        )

    async def _adjudicate_auto_analyze(self, request: SovereignRequest) -> SovereignOutcome:
        snapshot = self._last_snapshot or self._observe_domain()
        anomalies = self._analyze_domain(snapshot)
        self._auto_metrics["anomalies_detected"] += len(anomalies)
        if anomalies:
            self._pending_anomalies.extend(anomalies)
            self._auto_metrics["last_anomaly"] = self._iso_now()
        return accepted_outcome(
            {"action": "auto-analyze", "domain": "owned", "anomalies": anomalies},
            self.verified_basis("A20"),
        )

    async def _adjudicate_auto_manage(self, request: SovereignRequest) -> SovereignOutcome:
        actions = self._manage_domain()
        return accepted_outcome(
            {"action": "auto-manage", "domain": "owned", "actions": actions},
            self.verified_basis("A20"),
        )

    async def _adjudicate_auto_health_check(self, request: SovereignRequest) -> SovereignOutcome:
        self._auto_metrics["health_checks"] += 1
        anomalies = self._analyze_domain(self._last_snapshot or self._observe_domain())
        if anomalies:
            self._pending_anomalies.extend(anomalies)
            self._auto_metrics["last_anomaly"] = self._iso_now()
        return accepted_outcome(
            {"action": "auto-health-check", "domain": "owned", "anomalies": anomalies},
            self.verified_basis("A20"),
        )

    async def start_auto_loop(self) -> None:
        """Start the background auto-loop (A20 full-automation)."""
        if self._auto_loop_task is not None and not self._auto_loop_task.done():
            return
        self._auto_enabled = True
        self._auto_loop_task = asyncio.create_task(
            self._auto_loop(),
            name="xingcheng-auto-loop",
        )
        _logger.info("Xingcheng auto-loop started")

    async def stop_auto_loop(self) -> None:
        """Stop the auto-loop."""
        self._auto_enabled = False
        if self._auto_loop_task is not None:
            self._auto_loop_task.cancel()
            try:
                await self._auto_loop_task
            except asyncio.CancelledError:
                pass
            self._auto_loop_task = None
        _logger.info("Xingcheng auto-loop stopped")

    async def _auto_loop(self) -> None:
        """Background auto-loop: observe -> analyze -> reason -> manage."""
        # Batch size for anomaly processing
        batch_size = 10
        while self._auto_enabled:
            cycle_start = time.monotonic()
            try:
                # Observe
                snapshot = self._observe_domain()
                self._last_snapshot = snapshot
                self._auto_metrics["observe_cycles"] += 1

                # Analyze
                anomalies = self._analyze_domain(snapshot)
                if anomalies:
                    # Batch process anomalies to reduce notification overhead
                    for i in range(0, len(anomalies), batch_size):
                        batch = anomalies[i:i + batch_size]
                        self._pending_anomalies.extend(batch)
                        self._auto_metrics["anomalies_detected"] += len(batch)
                        self._auto_metrics["last_anomaly"] = self._iso_now()
                        await self._notify_anomalies(batch)
                    # A485: anomalies trigger a parent-commanded learning
                    # pass — the child only learns on 星澄's command.
                    commanded = await self.command_learning_pass("anomaly")
                    if commanded.get("commanded"):
                        self._auto_metrics["learning_commands"] += 1

                # Reason (internal advisory)
                self._auto_metrics["reason_cycles"] += 1

                # Manage
                actions = self._manage_domain()
                if actions:
                    self._auto_metrics["manage_cycles"] += 1

            except Exception as e:
                _logger.warning("Xingcheng auto-loop error: %s", e)

            # Adaptive sleep based on cycle duration
            cycle_duration = time.monotonic() - cycle_start
            sleep_time = max(0.1, self._auto_loop_interval - cycle_duration)

            try:
                await asyncio.sleep(sleep_time)
            except asyncio.CancelledError:
                break

    def auto_status(self) -> dict[str, Any]:
        return {
            "running": self._auto_loop_task is not None and not self._auto_loop_task.done(),
            "interval_seconds": self._auto_loop_interval,
            "enabled": self._auto_enabled,
            "metrics": dict(self._auto_metrics),
            "pending_anomalies": len(self._pending_anomalies),
        }