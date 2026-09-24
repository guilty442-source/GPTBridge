"""InvestmentIntelligenceEngine — the 星澄 AI 投資決策中心 facade.

Fully separated from execution: this engine produces analyses,
recommendations and validated proposals. It holds no reference to the
OMS, broker adapters or account mutation paths — proposals leave here
only as validated TradeProposal objects that must still pass the
strategy→risk→mode pipeline like any other candidate.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Awaitable, Callable

from ..accounts import AccountRegistry
from ..fund.engine import MutualFundEngine
from ..market.calendar import TradingCalendar
from ..market.engine import MarketDataEngine
from ..market.fx import CurrencyRateService
from ..portfolio_engine import PortfolioEngine
from .analyzers import TaiwanEquityIntelligence, USEquityIntelligence
from .contracts import (
    AnalysisTaskKind, RecommendationStatus,
)
from .evidence import AnalysisEvidenceService
from .fund_intel import MutualFundIntelligence
from .intent import InvestmentIntentParser
from .lifecycle import RecommendationLifecycle
from .outcome import RecommendationOutcomeService
from .pipeline import AnalysisPipeline
from .portfolio_intel import PortfolioIntelligence
from .proposal import ProposalFactory
from .router import ModelRouter
from .safety import AISafetyBoundary
from .scheduler import InvestmentAnalysisScheduler


class InvestmentIntelligenceEngine:
    def __init__(
        self, state_dir: Path,
        market_engine: MarketDataEngine,
        fund_engine: MutualFundEngine,
        portfolio: PortfolioEngine,
        accounts: AccountRegistry,
        fx: CurrencyRateService,
        calendar: TradingCalendar,
        consult: Callable[[str, str], Awaitable[dict[str, Any]]] | None = None,
        candle_store: Any | None = None,
    ) -> None:
        d = Path(state_dir)
        self.safety = AISafetyBoundary()
        self.evidence = AnalysisEvidenceService(d)
        self.router = ModelRouter(consult)
        self.pipeline = AnalysisPipeline(self.evidence, self.safety)
        self.intent = InvestmentIntentParser()
        self.lifecycle = RecommendationLifecycle(d)
        self.outcomes = RecommendationOutcomeService(
            d, market_engine, self.lifecycle, candle_store)
        self.scheduler = InvestmentAnalysisScheduler(d, calendar)
        self.proposals = ProposalFactory(self.safety)
        self.tw = TaiwanEquityIntelligence(
            market_engine, self.router, candle_store)
        self.us = USEquityIntelligence(
            market_engine, self.router, candle_store)
        self.fund_intel = MutualFundIntelligence(fund_engine, self.router)
        self.portfolio_intel = PortfolioIntelligence(
            portfolio, fund_engine, fx, self.router, accounts)
        self._calendar = calendar

    # ------------------------------------------------------------------
    def status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "model_available": self.router.available,
            "model_health": self.router.health(),
            "boundary": self.safety.boundary_manifest(),
            "execution": "separated — proposals only, never orders",
            "note": "模型故障不影響行情、持倉、風控",
        }
