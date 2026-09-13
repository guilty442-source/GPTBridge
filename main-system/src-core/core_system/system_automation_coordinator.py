"""System Automation Coordinator — 全系統自動化協調器。

法典依據:
- A63/A64: decision-only sovereigns, governed execution
- A128/A130: mother process delegates startup to decision-sovereign
- A152/A154: repair-decision chain
- A28: runtime-domain decision-only
- A20: 星澄 owned-domain isolation
- A33/A65: health coordination
- A66: information-layer channel
- A301/A322: synchronization adjudication
- A334: registry-driven child dispatch

功能:
1. 統一管理所有主宰的自動化循環（啟動/停止/監控）
2. 全系統健康監控（跨主宰狀態聚合）
3. 跨主宰協調（退化偵測 → 修復路由）
4. 全系統狀態介面（單一聚合視圖）
5. 自動化生命週期管理（啟動順序、關閉順序）

此協調器是**決策層**組件，不執行任何業務邏輯。它只協調已存在的
主宰自動化循環，不取代任何主宰的裁決權。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

_logger = logging.getLogger("gptbridge.system_automation")

# Coordination loop interval (seconds).
_COORDINATOR_INTERVAL_SECONDS = 15.0

# Sovereigns managed by the coordinator (in startup order).
_SOVEREIGN_ATTRS = (
    "decision_sovereign",
    "permission_sovereign",
    "system_runtime_sovereign",
    "synchronization_sovereign",
    "xingcheng_sovereign",
)


class SystemAutomationCoordinator:
    """全系統自動化協調器。

    統一管理所有主宰的自動化循環，提供全系統健康監控和跨主宰協調。
    此協調器是決策層組件，不執行業務邏輯，只協調已存在的自動化循環。
    """

    VERSION = "1.0.0"

    def __init__(self, app: Any) -> None:
        self.app = app
        self._running = False
        self._task: asyncio.Task[Any] | None = None
        self._stop_event = asyncio.Event()

        # Coordination metrics.
        self._metrics: dict[str, Any] = {
            "coordination_cycles": 0,
            "sovereigns_managed": 0,
            "sovereigns_automated": 0,
            "cross_sovereign_routes": 0,
            "health_aggregations": 0,
            "degradation_escalations": 0,
            "last_coordination_cycle": "",
            "last_degradation": "",
        }

        # Last aggregated system health.
        self._last_system_health: dict[str, Any] = {}

        # Pending cross-sovereign routes (degradation → repair).
        self._pending_routes: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Start the system automation coordinator.

        This does NOT start individual sovereign automation loops — those
        are started by each sovereign's own ``start()`` method via
        ``_on_start``.  This coordinator only starts the cross-sovereign
        coordination loop that aggregates health and routes degradation.
        """
        if self._running:
            return {"status": "already_running"}
        self._running = True
        self._stop_event.clear()
        self._metrics["sovereigns_managed"] = len(_SOVEREIGN_ATTRS)
        try:
            self._task = asyncio.create_task(
                self._coordination_loop(),
                name="system-automation-coordinator",
            )
        except RuntimeError:
            # No running event loop — coordination stays off.
            self._task = None
            self._running = False
            return {"status": "no_event_loop"}
        _logger.info("SystemAutomationCoordinator started")
        return {
            "status": "started",
            "sovereigns_managed": len(_SOVEREIGN_ATTRS),
            "interval_seconds": _COORDINATOR_INTERVAL_SECONDS,
        }

    async def stop(self) -> None:
        """Stop the system automation coordinator."""
        if not self._running:
            return
        self._running = False
        self._stop_event.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        _logger.info("SystemAutomationCoordinator stopped")

    # ------------------------------------------------------------------
    # Coordination loop
    # ------------------------------------------------------------------

    async def _coordination_loop(self) -> None:
        """Background loop: periodic cross-sovereign coordination."""
        while self._running and not self._stop_event.is_set():
            try:
                await self._coordination_cycle()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                _logger.warning(
                    "system automation coordination error: %s", error
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=_COORDINATOR_INTERVAL_SECONDS,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _coordination_cycle(self) -> None:
        """One coordination cycle: aggregate health, detect degradation, route."""
        self._metrics["coordination_cycles"] += 1
        self._metrics["last_coordination_cycle"] = self._iso_now()

        # 1. Aggregate system health from all sovereigns.
        health = self._aggregate_system_health()
        self._last_system_health = health
        self._metrics["health_aggregations"] += 1

        # 2. Count automated sovereigns.
        automated = sum(
            1 for s in self._sovereigns() if self._is_sovereign_automated(s)
        )
        self._metrics["sovereigns_automated"] = automated

        # 3. Detect cross-sovereign degradation.
        degradation = self._detect_degradation(health)
        if degradation:
            self._metrics["degradation_escalations"] += 1
            self._metrics["last_degradation"] = degradation.get(
                "type", ""
            )
            self._pending_routes.append(degradation)

        # 4. Route pending degradation to the decision-sovereign.
        await self._route_pending_degradations()

    def _sovereigns(self) -> list[Any]:
        """Get all managed sovereigns from the app."""
        result = []
        for attr in _SOVEREIGN_ATTRS:
            sov = getattr(self.app, attr, None)
            if sov is not None:
                result.append(sov)
        return result

    def _is_sovereign_automated(self, sovereign: Any) -> bool:
        """Check if a sovereign has its automation loop running."""
        # Check for auto loop task (sync, runtime, xingcheng).
        auto_task = getattr(sovereign, "_auto_loop_task", None)
        if auto_task is not None and not auto_task.done():
            return True
        # Check for autonomy task (decision sovereign).
        autonomy_task = getattr(sovereign, "_autonomy_task", None)
        if autonomy_task is not None and not autonomy_task.done():
            return True
        # Check for permission automation orchestrator.
        automation = getattr(sovereign, "_automation", None)
        if automation is not None and getattr(automation, "_running", False):
            return True
        return False

    def _aggregate_system_health(self) -> dict[str, Any]:
        """Aggregate health status from all managed sovereigns.

        Returns a unified health view combining:
        - Each sovereign's started state
        - Each sovereign's automation state
        - Each sovereign's live status
        - Overall system state
        """
        sovereigns_health: dict[str, Any] = {}
        all_started = True
        all_automated = True
        any_degraded = False
        any_critical = False

        for attr in _SOVEREIGN_ATTRS:
            sov = getattr(self.app, attr, None)
            if sov is None:
                sovereigns_health[attr] = {
                    "state": "missing",
                    "started": False,
                    "automated": False,
                }
                all_started = False
                all_automated = False
                any_critical = True
                continue

            started = bool(getattr(sov, "_started", False))
            automated = self._is_sovereign_automated(sov)
            sov_id = getattr(sov, "sovereign_id", attr)

            # Get sovereign-specific health.
            live_status = {}
            if hasattr(sov, "live_status"):
                try:
                    live_status = sov.live_status() or {}
                except Exception:
                    live_status = {}

            # Determine sovereign state.
            if not started:
                state = "stopped"
                any_critical = True
            else:
                # Check for degraded state.
                runtime_state = live_status.get("runtime_state", "")
                if runtime_state in ("degraded", "failed", "dead"):
                    state = "degraded"
                    any_degraded = True
                elif runtime_state in ("serving", "ready", "active"):
                    state = "healthy"
                else:
                    state = "started"

            sovereigns_health[attr] = {
                "sovereign_id": sov_id,
                "state": state,
                "started": started,
                "automated": automated,
                "runtime_state": runtime_state,
            }

            if not started:
                all_started = False
            if not automated:
                all_automated = False

        # Determine overall system state.
        if any_critical:
            overall = "critical"
        elif any_degraded:
            overall = "degraded"
        elif all_started and all_automated:
            overall = "fully-automated"
        elif all_started:
            overall = "running"
        else:
            overall = "partial"

        return {
            "aggregated_at": self._iso_now(),
            "overall_state": overall,
            "all_started": all_started,
            "all_automated": all_automated,
            "sovereigns": sovereigns_health,
            "coordinator_version": self.VERSION,
        }

    def _detect_degradation(
        self, health: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Detect cross-sovereign degradation that needs routing.

        Returns a degradation report if any sovereign is in a degraded
        or critical state, or None if the system is healthy.
        """
        overall = health.get("overall_state", "healthy")
        if overall in ("healthy", "fully-automated", "running"):
            return None

        degraded_sovereigns: list[dict[str, Any]] = []
        for attr, sov_health in health.get("sovereigns", {}).items():
            state = sov_health.get("state", "unknown")
            if state in ("degraded", "stopped", "critical"):
                degraded_sovereigns.append({
                    "sovereign": attr,
                    "sovereign_id": sov_health.get("sovereign_id", attr),
                    "state": state,
                    "runtime_state": sov_health.get("runtime_state", ""),
                })

        if not degraded_sovereigns:
            return None

        return {
            "detected_at": self._iso_now(),
            "type": "cross-sovereign-degradation",
            "overall_state": overall,
            "degraded_sovereigns": degraded_sovereigns,
            "route_to": "decision-sovereign.repair-decision",
            "authority": "system-automation-coordinator",
        }

    async def _route_pending_degradations(self) -> None:
        """Route pending degradation reports to the decision-sovereign.

        The decision-sovereign adjudicates the repair decision (A152/A154)
        and routes to the appropriate repair chain.  The coordinator only
        escalates the signal — it does not make repair decisions.
        """
        if not self._pending_routes:
            return

        decision = getattr(self.app, "decision_sovereign", None)
        if decision is None:
            _logger.warning(
                "degradation detected but decision-sovereign unavailable"
            )
            return

        from governance.sovereigns._base import SovereignRequest

        for route in self._pending_routes:
            try:
                request = SovereignRequest(
                    intent="repair.decide-and-route",
                    subject="system-automation-degradation",
                    requester="system-automation-coordinator",
                    payload={
                        "classified_signal": {
                            "repair_type": "cross-sovereign-degradation",
                            "target": "system-automation-coordinator",
                            "overall_state": route.get("overall_state"),
                            "degraded_sovereigns": route.get(
                                "degraded_sovereigns", []
                            ),
                        },
                    },
                )
                await decision.handle(request)
                self._metrics["cross_sovereign_routes"] += 1
            except Exception as error:
                _logger.warning(
                    "cross-sovereign degradation route failed: %s", error
                )

        # Clear pending routes after processing.
        self._pending_routes.clear()

    # ------------------------------------------------------------------
    # Status surfaces
    # ------------------------------------------------------------------

    def system_status(self) -> dict[str, Any]:
        """Get the full system automation status.

        This is the single aggregation point for all sovereign automation
        state.  It combines:
        - Coordinator metrics
        - Per-sovereign automation status
        - Aggregated system health
        - Pending cross-sovereign routes
        """
        sovereigns_status: dict[str, Any] = {}
        for attr in _SOVEREIGN_ATTRS:
            sov = getattr(self.app, attr, None)
            if sov is None:
                sovereigns_status[attr] = {
                    "state": "missing",
                    "automated": False,
                }
                continue

            sov_id = getattr(sov, "sovereign_id", attr)
            started = bool(getattr(sov, "_started", False))
            automated = self._is_sovereign_automated(sov)

            # Get sovereign-specific auto status.
            auto_status: dict[str, Any] = {}
            if hasattr(sov, "auto_status"):
                try:
                    auto_status = sov.auto_status() or {}
                except Exception:
                    pass
            elif hasattr(sov, "automation_status"):
                try:
                    auto_status = sov.automation_status() or {}
                except Exception:
                    pass

            sovereigns_status[attr] = {
                "sovereign_id": sov_id,
                "started": started,
                "automated": automated,
                "auto": auto_status,
            }

        return {
            "coordinator": {
                "version": self.VERSION,
                "running": self._running,
                "interval_seconds": _COORDINATOR_INTERVAL_SECONDS,
                "metrics": dict(self._metrics),
                "pending_routes": len(self._pending_routes),
            },
            "system_health": self._last_system_health or {
                "overall_state": "unknown",
                "sovereigns": {},
            },
            "sovereigns": sovereigns_status,
        }

    def _iso_now(self) -> str:
        """ISO-8601 UTC timestamp."""
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


__all__ = ["SystemAutomationCoordinator"]
