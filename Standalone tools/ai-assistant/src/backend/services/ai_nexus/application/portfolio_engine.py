from __future__ import annotations

from ..infrastructure.analytics_repository import InvestmentAnalyticsStore
from .portfolio_engine_actions import PortfolioEngineActionsMixin
from .portfolio_engine_analysis import PortfolioEngineAnalysisMixin
from .portfolio_engine_fx import PortfolioEngineFxMixin
from .portfolio_engine_simulation import PortfolioEngineSimulationMixin
from .portfolio_fx import (
    sync_factor_proxies_from_yahoo,
    sync_fx_from_huanan_bank,
    sync_fx_from_yahoo,
)


class InvestmentV3Engine(
    PortfolioEngineFxMixin,
    PortfolioEngineAnalysisMixin,
    PortfolioEngineSimulationMixin,
    PortfolioEngineActionsMixin,
):
    def __init__(self, store: InvestmentAnalyticsStore) -> None:
        self.store = store


__all__ = [
    "InvestmentV3Engine",
    "sync_fx_from_huanan_bank",
    "sync_fx_from_yahoo",
    "sync_factor_proxies_from_yahoo",
]
