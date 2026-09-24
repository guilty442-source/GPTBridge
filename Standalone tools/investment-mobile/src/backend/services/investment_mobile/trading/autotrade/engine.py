"""AutoTradingEngine — facade for the autonomous simulated-trading
center.

Composes: event dispatcher, runtime state machine + multi-strategy
manager, capital allocator, resource coordinator, scheduler, session
controller, pipeline coordinator, SHADOW/PAPER autonomous loops, AI
signal integration + workload manager, strategy risk monitor, auto-halt,
recovery, performance monitor, stability analyzer, improvement research,
experiment manager, overfitting guard, fund coordinator, reports and
maintenance.

Everything operates on the simulation stack — SHADOW records signals,
PAPER fills through the mock broker contract. No path here reaches a
real broker: BrokerGateway is never consulted, the offline gate stays
locked, and LIVE dispatch stays disabled.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .ai_integration import (AIAnalysisWorkloadManager,
                             AISignalIntegrationService)
from .capital import (StrategyCapitalAllocator,
                      StrategyResourceCoordinator)
from .coordinator import AutoTradingCoordinator
from .events import TradingEventDispatcher
from .fund import FundStrategyCoordinator
from .maintenance import AutoTradingMaintenanceService
from .performance import (StrategyPerformanceMonitor,
                          StrategyStabilityAnalyzer)
from .reports import AutonomousTradingReport
from .research import (AIStrategyImprovementService,
                       StrategyExperimentManager,
                       StrategyOverfittingGuard)
from .risk import (StrategyAutoHaltService, StrategyRecoveryService,
                   StrategyRiskMonitor)
from .runtime import MultiStrategyManager, RuntimeState
from .scheduler import (StrategyScheduler,
                        TradingSessionController)


class AutoTradingEngine:
    def __init__(self, state_dir: Path, *, sim: Any,
                 calendar: Any, monitoring: Any, intel: Any,
                 strategy_registry: Any, fund_engine: Any) -> None:
        state_dir = Path(state_dir)
        self.dispatcher = TradingEventDispatcher(state_dir)
        self.manager = MultiStrategyManager(state_dir)
        self.allocator = StrategyCapitalAllocator(state_dir)
        self.resources = StrategyResourceCoordinator(state_dir)
        self.sessions = TradingSessionController(calendar)
        self.scheduler = StrategyScheduler(state_dir, calendar)
        self.workload = AIAnalysisWorkloadManager(
            getattr(monitoring, "orchestrator", None))
        self.ai_integration = AISignalIntegrationService(self.workload)
        self.risk_monitor = StrategyRiskMonitor(
            sim, monitoring.gate, monitoring.events)
        self.halt = StrategyAutoHaltService(
            self.manager, monitoring.notifications)
        self.recovery = StrategyRecoveryService(
            self.manager, sim, monitoring.gate)
        self.performance = StrategyPerformanceMonitor(sim, state_dir)
        self.stability = StrategyStabilityAnalyzer(self.performance)
        self.improvement = AIStrategyImprovementService(
            state_dir, strategy_registry, self.workload)
        self.experiments = StrategyExperimentManager(state_dir)
        self.overfitting = StrategyOverfittingGuard(state_dir)
        self.fund_coordinator = FundStrategyCoordinator(
            fund_engine, monitoring.recommend, monitoring.events)
        self.reports = AutonomousTradingReport(state_dir)
        self.maintenance = AutoTradingMaintenanceService(state_dir)
        self.coordinator = AutoTradingCoordinator(
            manager=self.manager, dispatcher=self.dispatcher,
            allocator=self.allocator, resources=self.resources,
            sessions=self.sessions, scheduler=self.scheduler,
            gate=monitoring.gate, sim=sim,
            ai_integration=self.ai_integration,
            events=monitoring.events,
            risk_monitor=self.risk_monitor)
        self.sim = sim
        self._monitoring = monitoring
        self._registry = strategy_registry

    # ------------------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        runs = self.manager.list()
        by_state: dict[str, int] = {}
        for r in runs:
            by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        return {
            "ok": True,
            "strategies": len(runs),
            "by_state": by_state,
            "jobs": self.scheduler.status()["jobs"],
            "open_events": len(self._monitoring.events.list(
                status="open")),
            "model_available": self.workload.stats()["orchestrator"].get(
                "model_available", False),
            "mode": "SHADOW/PAPER only — LIVE stays phase-locked",
        }

    async def on_market_event(self, event_type: str, *,
                              market: str, instrument_id: str = "",
                              source_id: str = "market",
                              data_revision: str = "",
                              timestamp: float | None = None
                              ) -> dict[str, Any]:
        """Publish → dispatch to running strategies; idempotent per
        event fingerprint per strategy."""
        pub = self.dispatcher.publish(
            event_type, market=market, instrument_id=instrument_id,
            source_id=source_id, data_revision=data_revision,
            timestamp=timestamp)
        if not pub.get("ok"):
            return pub
        if pub.get("duplicate"):
            return {"ok": True, "duplicate": True,
                    "event": pub["event"], "cycles": []}
        event = pub["event"]
        cycles = []
        for run in self.manager.list(state=RuntimeState.RUNNING):
            if run["market"] and market and run["market"] != market:
                continue
            r = await self.coordinator.run_cycle(run["run_id"], event)
            cycles.append({"run_id": run["run_id"], **r})
        # scheduled event-driven jobs
        jobs = self.scheduler.on_event(event)
        return {"ok": True, "event": event, "cycles": cycles,
                "scheduled_jobs": jobs.get("due", [])}

    def close(self) -> None:
        try:
            self.dispatcher.close()
        except Exception:
            pass
