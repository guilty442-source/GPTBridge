"""UnifiedPortfolioEngine — the asset-center facade.

Composes: account registry, positions, cost basis, transaction ledger,
cash, currency conversion, valuation, performance, income, allocation,
exposure, risk analytics, snapshots, environment isolation, 星澄
intelligence surface, recommendations and maintenance — all over the
single offline-accounts journal (no second authority).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .accounts import InvestmentAccountService
from .allocation import AssetAllocationEngine
from .cash import CashManagementService
from .cost import CostBasisEngine
from .currency import CurrencyConversionEngine
from .exposure import PortfolioExposureEngine
from .income import InvestmentIncomeService
from .intelligence import (PortfolioIntelligenceService,
                           PortfolioRecommendationService)
from .isolation import (PortfolioEnvironment,
                        PortfolioEnvironmentIsolation)
from .ledger import InvestmentTransactionLedger
from .maintenance import PortfolioMaintenanceService
from .performance import PortfolioPerformanceEngine
from .positions import PositionManagementService
from .risk_analytics import PortfolioRiskAnalytics
from .snapshots import PortfolioSnapshotService
from .valuation import PortfolioValuationEngine


class UnifiedPortfolioEngine:
    def __init__(self, state_dir: Path, offline: Any, fx: Any,
                 candle_store: Any | None = None,
                 sim: Any | None = None, live: Any | None = None,
                 cost_estimator: Any | None = None) -> None:
        self.registry = InvestmentAccountService(state_dir)
        self.positions = PositionManagementService(state_dir, offline)
        self.ledger = InvestmentTransactionLedger(offline)
        self.cash = CashManagementService(state_dir, offline)
        self.ccy = CurrencyConversionEngine(fx)
        self.cost = CostBasisEngine(self.ledger)
        self.valuation = PortfolioValuationEngine(
            self.positions, self.cash, self.ccy, candle_store)
        self.income = InvestmentIncomeService(offline)
        self.performance = PortfolioPerformanceEngine(
            self.ledger, self.income)
        self.allocation = AssetAllocationEngine(state_dir)
        self.exposure = PortfolioExposureEngine(state_dir)
        self.snapshots = PortfolioSnapshotService(state_dir)
        self.risk_analytics = PortfolioRiskAnalytics(self.snapshots)
        self.isolation = PortfolioEnvironmentIsolation(
            offline, sim=sim, live=live)
        self.intelligence = PortfolioIntelligenceService(
            self.valuation, self.allocation, self.exposure,
            self.risk_analytics, self.income, self.performance)
        self.recommendations = PortfolioRecommendationService(
            self.allocation, cost_estimator)
        self.maintenance = PortfolioMaintenanceService(state_dir)

    # ------------------------------------------------------------------
    def value(self, **kwargs) -> dict[str, Any]:
        return self.valuation.value(**kwargs)

    def close(self) -> None:
        for svc in (self.snapshots,):
            try:
                svc.close()
            except Exception:
                pass
