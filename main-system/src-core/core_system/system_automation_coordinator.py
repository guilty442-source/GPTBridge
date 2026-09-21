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

A337 邊界：全系統自動化**任務執行**（分解/排程/派工/收斂/驗收/故障
隔離）專屬於星澄的 automation executor；本協調器只做主宰生命週期
與健康聚合，不是平行的自動化協調器（A337 prohibition）。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from .system_automation_coordinator_constants import (
    _COORDINATOR_INTERVAL_SECONDS,
    _SOVEREIGN_ATTRS,
)
from .system_automation_coordinator_health import SystemAutomationHealthMixin

_logger = logging.getLogger("gptbridge.system_automation")


class SystemAutomationCoordinator(SystemAutomationHealthMixin):
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

        self._last_system_health: dict[str, Any] = {}
        self._pending_routes: list[dict[str, Any]] = []
        # Circuit breaker for cascade failure prevention
        self._consecutive_errors = 0
        self._circuit_breaker_threshold = 5
        self._circuit_open_until = 0.0
        # Adaptive interval: increase when system is healthy
        self._adaptive_interval = _COORDINATOR_INTERVAL_SECONDS
        self._min_interval = _COORDINATOR_INTERVAL_SECONDS
        self._max_interval = 300.0  # Max 5 minutes
        self._consecutive_healthy = 0
        # §10.63 R3: the cycle runs on the shared PeriodicScheduler; the
        # adaptive interval gates cycles inside the tick instead of
        # sleeping a private task.
        self._last_cycle_monotonic = 0.0

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Start the system automation coordinator."""
        if self._running:
            return {"status": "already_running"}
        self._running = True
        self._stop_event.clear()
        self._metrics["sovereigns_managed"] = len(_SOVEREIGN_ATTRS)
        scheduler = getattr(self.app, "periodic_scheduler", None)
        if scheduler is not None:
            # §10.63 R3: one shared loop instead of a private task.
            scheduler.register(
                "system-automation-coordinator",
                _COORDINATOR_INTERVAL_SECONDS,
                self._scheduled_tick,
                run_immediately=True,
            )
        else:
            try:
                self._task = asyncio.create_task(
                    self._coordination_loop(),
                    name="system-automation-coordinator",
                )
            except RuntimeError:
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
        scheduler = getattr(self.app, "periodic_scheduler", None)
        if scheduler is not None:
            scheduler.unregister("system-automation-coordinator")
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

    async def _scheduled_tick(self) -> None:
        """One guarded coordination iteration for the shared scheduler.

        Preserves the private loop's semantics: the adaptive interval gates
        cycle frequency, the circuit breaker isolates repeated failures,
        and errors are counted and logged — never propagated into the
        scheduler loop.
        """
        if not self._running or self._stop_event.is_set():
            return
        import time

        now = time.monotonic()
        if now - self._last_cycle_monotonic < self._adaptive_interval:
            return
        if self._consecutive_errors >= self._circuit_breaker_threshold:
            if time.time() < self._circuit_open_until:
                return
            self._consecutive_errors = 0
            self._circuit_open_until = 0.0
            _logger.info(
                "SystemAutomationCoordinator circuit breaker reset"
            )
        self._last_cycle_monotonic = now
        try:
            await self._coordination_cycle()
            self._consecutive_errors = 0
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._consecutive_errors += 1
            if self._consecutive_errors >= self._circuit_breaker_threshold:
                self._circuit_open_until = time.time() + 300  # 5 minutes
                _logger.warning(
                    "SystemAutomationCoordinator circuit breaker opened for 5 minutes after %d errors",
                    self._consecutive_errors,
                )
            _logger.warning(
                "system automation coordination error: %s", error
            )

    async def _coordination_loop(self) -> None:
        """Background loop: periodic cross-sovereign coordination with circuit breaker and adaptive interval."""
        import time
        while self._running and not self._stop_event.is_set():
            try:
                # Circuit breaker check
                if self._consecutive_errors >= self._circuit_breaker_threshold:
                    if time.time() < self._circuit_open_until:
                        _logger.warning(
                            "SystemAutomationCoordinator circuit breaker open, waiting %.0fs",
                            self._circuit_open_until - time.time(),
                        )
                        await asyncio.sleep(60)
                        continue
                    else:
                        # Reset circuit breaker after timeout
                        self._consecutive_errors = 0
                        self._circuit_open_until = 0.0
                        _logger.info("SystemAutomationCoordinator circuit breaker reset")

                await self._coordination_cycle()
                # Success - reset error count
                self._consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._consecutive_errors += 1
                if self._consecutive_errors >= self._circuit_breaker_threshold:
                    self._circuit_open_until = time.time() + 300  # 5 minutes
                    _logger.warning(
                        "SystemAutomationCoordinator circuit breaker opened for 5 minutes after %d errors",
                        self._consecutive_errors,
                    )
                _logger.warning(
                    "system automation coordination error: %s", error
                )
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._adaptive_interval,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _coordination_cycle(self) -> None:
        """One coordination cycle: aggregate health, detect degradation, route."""
        self._metrics["coordination_cycles"] += 1
        self._metrics["last_coordination_cycle"] = self._iso_now()

        health = self._aggregate_system_health()
        self._last_system_health = health
        self._metrics["health_aggregations"] += 1

        automated = sum(
            1 for s in self._sovereigns() if self._is_sovereign_automated(s)
        )
        self._metrics["sovereigns_automated"] = automated

        degradation = self._detect_degradation(health)
        if degradation:
            self._metrics["degradation_escalations"] += 1
            self._metrics["last_degradation"] = degradation.get("type", "")
            self._pending_routes.append(degradation)
            # Reset adaptive interval on degradation
            self._consecutive_healthy = 0
            self._adaptive_interval = self._min_interval
        else:
            # System is healthy - increase interval
            self._consecutive_healthy += 1
            if self._consecutive_healthy >= 3:
                self._adaptive_interval = min(
                    self._adaptive_interval * 1.5,
                    self._max_interval,
                )

        await self._route_pending_degradations()
        self._emit_xingcheng_evidence(health)

    def _sovereigns(self) -> list[Any]:
        """Get all managed sovereigns from the app."""
        result = []
        for attr in _SOVEREIGN_ATTRS:
            sov = getattr(self.app, attr, None)
            if sov is not None:
                result.append(sov)
        return result

    async def _route_pending_degradations(self) -> None:
        """Route pending degradation reports to the decision-sovereign."""
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

        self._pending_routes.clear()

    # ------------------------------------------------------------------
    # Status surfaces
    # ------------------------------------------------------------------

    def system_status(self) -> dict[str, Any]:
        """Get the full system automation status."""
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
