"""星澄 AI 自動投資監測中心 — monitoring, alerting, advisory
recommendations, reports. Emits information; never executes trades."""

from .alerts import InvestmentAlertEngine, SEVERITY_ORDER
from .data_gate import MonitoringDataGate
from .engine import InvestmentMonitoringEngine
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

__all__ = [
    "CrossMarketExposureMonitor",
    "InvestmentAlertEngine",
    "InvestmentAnalysisOrchestrator",
    "InvestmentMonitoringEngine",
    "InvestmentMonitoringScheduler",
    "InvestmentNotificationService",
    "InvestmentOpportunityScanner",
    "InvestmentRecommendationEngine",
    "InvestmentReportService",
    "MonitoringDataGate",
    "MonitoringEventStore",
    "MonitoringMaintenanceService",
    "MutualFundMonitor",
    "PortfolioReallocationService",
    "PortfolioRiskMonitor",
    "SEVERITY_ORDER",
    "TaiwanInvestmentMonitor",
    "USInvestmentMonitor",
]
