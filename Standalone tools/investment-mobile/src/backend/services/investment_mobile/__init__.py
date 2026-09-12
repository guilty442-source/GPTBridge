"""Investment Mobile Service Package."""

from __future__ import annotations

from .application import AnalyzeInvestmentUseCase, ManagePortfolioUseCase
from .domain import InvestmentPortfolio, MarketData
from .infrastructure import DatabaseClient, MarketDataClient
from .integration import ChannelClient, ExternalAPIClient
from .presentation import InvestmentMobilePresenter, PortfolioPresenter

__version__ = "1.0.0"

__all__ = [
    "AnalyzeInvestmentUseCase",
    "ChannelClient",
    "DatabaseClient",
    "ExternalAPIClient",
    "InvestmentMobilePresenter",
    "InvestmentPortfolio",
    "ManagePortfolioUseCase",
    "MarketData",
    "MarketDataClient",
    "PortfolioPresenter",
]