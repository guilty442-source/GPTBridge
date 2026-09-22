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
from .codex_drift import DRIFT_CHECK_EVERY_CYCLES

_logger = logging.getLogger("gptbridge.sovereign.xingcheng.auto")

# Maintenance (sqlite quick_check / cache prune / config validation) runs on a
# wall-clock gate instead of every cycle — it is idempotent housekeeping, not
# per-cycle observation. The gate preserves the former 30-cycle × 10s cadence.
_MANAGE_MIN_INTERVAL_SECONDS = 30 * 10.0
# Drift review preserves the former DRIFT_CHECK_EVERY_CYCLES × 10s cadence.
_DRIFT_MIN_INTERVAL_SECONDS = DRIFT_CHECK_EVERY_CYCLES * 10.0
# §10.63 R3: the auto-loop is event-driven with a 60s minimum cadence — new
# faults/examples wake it early; idle polling at 10s is retired.
_AUTO_LOOP_MIN_INTERVAL_SECONDS = 60.0

# 星澄的模型與人格升級屬於 owned-domain 內部生命週期，不受星澄助理的
# system update release switch 控制。該開關只控制主系統更新流程。
_SELF_UPGRADE_OWNER = "xingcheng"
_SELF_UPGRADE_HANDLING = "owned-domain-internal"


class XingchengAutoMixin:
    """Full-automation upgrade — auto-loop inside owned domain."""

    _auto_loop_task: asyncio.Task[Any] | None
    _auto_loop_interval: float
    _auto_enabled: bool
    _auto_metrics: dict[str, Any]
    _last_snapshot: dict[str, Any]
    _pending_anomalies: list[dict[str, Any]]
    _learning_armed: bool
    _auto_wake: asyncio.Event | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._auto_loop_task = None
        self._auto_loop_interval = _AUTO_LOOP_MIN_INTERVAL_SECONDS
        self._auto_wake: asyncio.Event | None = None
        self._last_manage_at = 0.0
        self._last_drift_at = 0.0
        self._auto_enabled = True
        self._auto_metrics = {
            "observe_cycles": 0, "analyze_cycles": 0, "reason_cycles": 0,
            "manage_cycles": 0, "health_checks": 0, "anomalies_detected": 0,
            "channel_notifications": 0, "db_maintenance_runs": 0,
            "model_loads": 0, "config_updates": 0, "learning_commands": 0,
            "self_upgrade_owner": _SELF_UPGRADE_OWNER,
            "self_upgrade_handling": _SELF_UPGRADE_HANDLING,
            "internal_fault_notifications": 0,
            "autonomous_repairs_requested": 0,
            "last_auto_cycle": "", "last_anomaly": "",
        }
        self._last_snapshot = {}
        self._pending_anomalies = []
        self._drift_cycle_counter = 0
        self._manage_cycle_counter = 0
        self._last_error_signature: str | None = None

    def handle_internal_fault_repair(
        self, classified_signal: dict[str, Any]
    ) -> dict[str, Any]:
        """Receive an internal fault notice and autonomously start repair.

        星澄 owns the autonomous maintenance request. Permission validation,
        mutation and verification remain inside the governed repair chain.
        The fault is not converted into a user-confirmation message.
        """
        self._auto_metrics["internal_fault_notifications"] += 1
        self.request_auto_cycle("internal-fault")
        decision_sovereign = getattr(self.app, "decision_sovereign", None)
        route = getattr(decision_sovereign, "decide_and_route_repair", None)
        if not callable(route):
            return {
                "ok": False,
                "decision": "denied-no-governed-repair-route",
                "notified_to": self.sovereign_id,
                "autonomous_owner": self.sovereign_id,
            }
        self._auto_metrics["autonomous_repairs_requested"] += 1
        result = dict(route(dict(classified_signal)))
        result["notified_to"] = self.sovereign_id
        result["autonomous_owner"] = self.sovereign_id
        result["notification_channel"] = "internal-information-layer"
        result["user_confirmation"] = "not-required"
        result["assistant_notification"] = "status-only"
        return result

    async def _adjudicate_auto_observe(self, request: SovereignRequest) -> SovereignOutcome:
        snapshot = await asyncio.to_thread(self._observe_domain)
        self._last_snapshot = snapshot
        return accepted_outcome(
            {"action": "auto-observe", "domain": "owned", "snapshot": snapshot},
            self.verified_basis("A20"),
        )

    async def _adjudicate_auto_analyze(self, request: SovereignRequest) -> SovereignOutcome:
        snapshot = self._last_snapshot or await asyncio.to_thread(self._observe_domain)
        anomalies = await asyncio.to_thread(self._analyze_domain, snapshot)
        self._auto_metrics["anomalies_detected"] += len(anomalies)
        if anomalies:
            self._pending_anomalies.extend(anomalies)
            self._auto_metrics["last_anomaly"] = self._iso_now()
        return accepted_outcome(
            {"action": "auto-analyze", "domain": "owned", "anomalies": anomalies},
            self.verified_basis("A20"),
        )

    async def _adjudicate_auto_manage(self, request: SovereignRequest) -> SovereignOutcome:
        actions = await asyncio.to_thread(self._manage_domain)
        return accepted_outcome(
            {"action": "auto-manage", "domain": "owned", "actions": actions},
            self.verified_basis("A20"),
        )

    async def _adjudicate_auto_health_check(self, request: SovereignRequest) -> SovereignOutcome:
        self._auto_metrics["health_checks"] += 1
        anomalies = await asyncio.to_thread(
            self._analyze_domain,
            self._last_snapshot or await asyncio.to_thread(self._observe_domain),
        )
        if anomalies:
            self._pending_anomalies.extend(anomalies)
            self._auto_metrics["last_anomaly"] = self._iso_now()
        return accepted_outcome(
            {"action": "auto-health-check", "domain": "owned", "anomalies": anomalies},
            self.verified_basis("A20"),
        )

    def request_auto_cycle(self, reason: str = "") -> None:
        """Event-driven wake (§10.63 R3): new faults/examples run a cycle
        early; without events the loop holds the 60s minimum cadence."""
        if self._auto_wake is not None:
            self._auto_wake.set()

    async def start_auto_loop(self) -> None:
        """Start the background auto-loop (A20 full-automation)."""
        if self._auto_loop_task is not None and not self._auto_loop_task.done():
            return
        self._auto_enabled = True
        self._auto_wake = asyncio.Event()
        self._auto_loop_task = asyncio.create_task(
            self._auto_loop(),
            name="xingcheng-auto-loop",
        )
        _logger.info("Xingcheng auto-loop started")

    async def stop_auto_loop(self) -> None:
        """Stop the auto-loop."""
        self._auto_enabled = False
        if self._auto_wake is not None:
            self._auto_wake.set()
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
        # Wall-clock gates preserve the former cycle-count cadences and keep
        # them correct under event-driven early wakes.
        self._last_manage_at = time.monotonic()
        self._last_drift_at = time.monotonic()
        while self._auto_enabled:
            cycle_start = time.monotonic()
            if self._auto_wake is not None:
                self._auto_wake.clear()
            try:
                # A485 commanded learning: retry the parent command until
                # the learning child is materialized/started (startup order
                # can make the first command fail closed).
                if not self._learning_armed:
                    await self.ensure_learning_automation()

                # Observe -> analyze -> manage: synchronous FS/sqlite work runs
                # off the event loop; maintenance runs on a wall-clock gate.
                manage_due = (
                    time.monotonic() - self._last_manage_at
                    >= _MANAGE_MIN_INTERVAL_SECONDS
                )
                if manage_due:
                    self._last_manage_at = time.monotonic()
                snapshot, anomalies, actions = await asyncio.to_thread(
                    self._domain_cycle, manage_due
                )
                self._last_snapshot = snapshot

                if anomalies:
                    await self._process_anomalies(anomalies, batch_size)
                    # A485: anomalies trigger a parent-commanded learning
                    # pass — the child only learns on 星澄's command.
                    commanded = await self.command_learning_pass("anomaly")
                    if commanded.get("commanded"):
                        self._auto_metrics["learning_commands"] += 1

                # Reason (internal advisory)
                self._auto_metrics["reason_cycles"] += 1

                # A145: periodic codex-vs-implementation drift review,
                # displayed on the 星澄 auxiliary surface (advisory).
                await self._run_drift_review()
                self._last_error_signature = None

            except Exception as e:
                # 暫態失敗（如 live codex 修訂期 CODEX_UNAVAILABLE）在
                # 60s 週期下每圈一行 error 會撐爆 INT-10 日誌衛生——
                # 同一錯誤簽名首次 warning、重複降 debug；乾淨週期後
                # 再發生重新 warning（不吞掉真實錯誤）。
                signature = f"{type(e).__name__}: {e}"
                if signature != self._last_error_signature:
                    _logger.warning("Xingcheng auto-loop error: %s", e)
                    self._last_error_signature = signature
                else:
                    _logger.debug(
                        "xingcheng auto-loop repeat failure: %s", e)

            # Event-driven wait: sleep until the 60s minimum cadence expires
            # or an event (fault/example/anomaly) requests an early cycle.
            cycle_duration = time.monotonic() - cycle_start
            sleep_time = max(0.1, self._auto_loop_interval - cycle_duration)

            try:
                if self._auto_wake is not None:
                    await asyncio.wait_for(
                        self._auto_wake.wait(), timeout=sleep_time
                    )
                else:
                    await asyncio.sleep(sleep_time)
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                break

    async def _process_anomalies(
        self, anomalies: list[dict[str, Any]], batch_size: int
    ) -> None:
        """Batch-record anomalies and notify through the auxiliary surface."""
        for i in range(0, len(anomalies), batch_size):
            batch = anomalies[i:i + batch_size]
            self._pending_anomalies.extend(batch)
            self._auto_metrics["anomalies_detected"] += len(batch)
            self._auto_metrics["last_anomaly"] = self._iso_now()
            await self._notify_anomalies()

    async def _run_drift_review(self) -> None:
        """Periodic codex-vs-implementation drift review (A145, advisory)."""
        if (
            time.monotonic() - self._last_drift_at
            < _DRIFT_MIN_INTERVAL_SECONDS
        ):
            return
        self._last_drift_at = time.monotonic()
        report = await asyncio.to_thread(self.run_codex_drift_check)
        self._auto_metrics["drift_checks"] = (
            self._auto_metrics.get("drift_checks", 0) + 1
        )
        self._auto_metrics["drift_findings"] = report.get("drift_count", 0)
        if self._pending_anomalies:
            await self._notify_anomalies()

    def auto_status(self) -> dict[str, Any]:
        return {
            "running": self._auto_loop_task is not None and not self._auto_loop_task.done(),
            "interval_seconds": self._auto_loop_interval,
            "enabled": self._auto_enabled,
            "metrics": dict(self._auto_metrics),
            "pending_anomalies": len(self._pending_anomalies),
            "self_upgrade": {
                "owner": _SELF_UPGRADE_OWNER,
                "handling": _SELF_UPGRADE_HANDLING,
                "user_switch": False,
                "assistant_release_switch": False,
                "scope": "xingcheng-owned-domain-only",
            },
        }
