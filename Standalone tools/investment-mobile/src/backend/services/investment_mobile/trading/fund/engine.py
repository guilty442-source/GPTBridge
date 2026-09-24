"""MutualFundEngine — composes the fund domain services.

Owns fund identity, classification, providers, NAV, distributions,
fees, performance, comparison, exposure, transactions, cost basis,
recurring plans, recommendations and strategies. It NEVER controls the
stock trading engines — shared concerns (FX, calendar, accounts) come
through the existing trading core; fund orders stay out of the OMS in
this phase (live fund trading is disabled by policy).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..market.fx import CurrencyRateService
from .comparison import FundComparisonService
from .cost import FundCostBasisService
from .distribution import FundDistributionService
from .exposure import FundExposureEngine, UnifiedPortfolioExposure
from .fees import FundFeeEngine
from .identity import FundIdentityRegistry
from .nav import FundNAVService
from .performance import FundPerformanceEngine
from .providers import PROVIDER_CAPABILITIES
from .recommendation import FundRecommendationService
from .recurring import FundRecurringInvestmentService
from .strategy import FundStrategyEngine
from .transactions import FundTransactionService


class MutualFundEngine:
    def __init__(self, state_dir: Path, fx: CurrencyRateService) -> None:
        state_dir = Path(state_dir)
        self.identities = FundIdentityRegistry(state_dir)
        self.nav = FundNAVService(state_dir)
        self.distributions = FundDistributionService(state_dir)
        self.fees = FundFeeEngine(state_dir)
        self.performance = FundPerformanceEngine(self.nav, self.distributions)
        self.comparison = FundComparisonService(
            self.identities, self.nav, self.distributions, self.fees,
            self.performance, fx,
        )
        self.exposure = FundExposureEngine(state_dir)
        self.unified = UnifiedPortfolioExposure(self.exposure)
        self.transactions = FundTransactionService(state_dir)
        self.cost_basis = FundCostBasisService(self.transactions, self.nav)
        self.recurring = FundRecurringInvestmentService(
            state_dir, self.transactions, self.cost_basis, self.performance)
        self.recommendations = FundRecommendationService(state_dir, self.nav)
        self.strategies = FundStrategyEngine(state_dir)

    def capability_matrix(self) -> list[dict[str, Any]]:
        return [c.to_dict() for c in PROVIDER_CAPABILITIES.values()]

    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "funds": len(self.identities.list()),
            "providers": len(PROVIDER_CAPABILITIES),
            "live_trading": "disabled",
            "note": "基金實盤交易停用——本階段僅分析與建議",
        }
