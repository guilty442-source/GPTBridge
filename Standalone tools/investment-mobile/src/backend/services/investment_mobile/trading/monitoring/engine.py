"""InvestmentMonitoringEngine — facade composing the monitoring center.

Reuses: CandleStore/TradingCalendar (market), MutualFundEngine (fund),
CurrencyRateService (fx), UnifiedPortfolioEngine (assets), and the
intelligence cluster (RecommendationLifecycle, OutcomeService,
AnalysisScheduler, ModelRouter, IndicatorSet). Nothing here can trade —
the layer emits events, alerts, notifications, reports and advisory
recommendations only.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .alerts import InvestmentAlertEngine
from .data_gate import MonitoringDataGate
from .events import MonitoringEventStore
from .maintenance import MonitoringMaintenanceService
from .monitors import (MutualFundMonitor, TaiwanInvestmentMonitor,
                       USInvestmentMonitor)
from .notifications import InvestmentNotificationService
from .orchestrator import InvestmentAnalysisOrchestrator
from .reallocation import PortfolioReallocationService
from .recommend import InvestmentRecommendationEngine
from .reports import InvestmentReportService
from .risk_monitor import (CrossMarketExposureMonitor,
                           PortfolioRiskMonitor)
from .scanner import InvestmentOpportunityScanner
from .scheduler import InvestmentMonitoringScheduler


class InvestmentMonitoringEngine:
    def __init__(self, state_dir: Path, *, candle_store: Any,
                 calendar: Any, market_engine: Any, fund_engine: Any,
                 fx: Any, assets: Any, intel: Any,
                 instruments: Any,
                 cost_estimator: Any | None = None) -> None:
        self.events = MonitoringEventStore(state_dir)
        self.gate = MonitoringDataGate(
            market_engine, candle_store, fx, fund_engine)
        self.tw_monitor = TaiwanInvestmentMonitor(
            candle_store, calendar, self.events, self.gate)
        self.us_monitor = USInvestmentMonitor(
            candle_store, calendar, self.events, self.gate)
        self.fund_monitor = MutualFundMonitor(
            fund_engine, self.events, self.gate)
        self.alerts = InvestmentAlertEngine(state_dir, self.events)
        self.notifications = InvestmentNotificationService(state_dir)
        self.recommend = InvestmentRecommendationEngine(
            state_dir, intel.lifecycle, intel.outcomes, intel.router)
        self.scanner = InvestmentOpportunityScanner(
            candle_store, instruments, fund_engine)
        self.risk_monitor = PortfolioRiskMonitor(
            assets.risk_analytics, self.events)
        self.cross_market = CrossMarketExposureMonitor(
            assets.exposure, self.events)
        self.reallocation = PortfolioReallocationService(
            state_dir, assets.allocation, cost_estimator)
        self.reports = InvestmentReportService(state_dir)
        self.orchestrator = InvestmentAnalysisOrchestrator(intel.router)
        self.scheduler = InvestmentMonitoringScheduler(
            intel.scheduler, state_dir)
        self.maintenance = MonitoringMaintenanceService(state_dir)
        self._assets = assets
        self._intel = intel

    # ------------------------------------------------------------------
    def overview(self) -> dict[str, Any]:
        return {
            "ok": True,
            "model_available": self.orchestrator.stats()[
                "model_available"],
            "open_events": len(self.events.list(status="open")),
            "unread_notifications": len(self.notifications.history(
                unread_only=True)),
            "rules": len(self.alerts.list_rules()),
            "schedules": self.scheduler.status()["schedules"],
        }

    def close(self) -> None:
        for svc in (self.events, self.notifications):
            try:
                svc.close()
            except Exception:
                pass
