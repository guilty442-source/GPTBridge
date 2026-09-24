"""星澄 AI 投資管理與自動操盤系統 — trading engine package.

Decision-free governed engines physically hosted inside the
``investment-mobile`` tool boundary, organised as 11 logical domains
(see ``domains.DOMAINS``):

  market-data  instrument  portfolio  strategy  risk  trading
  broker       mutual-fund ai-analysis backtest  audit

Pipeline: MarketData → Strategy → TradingSignal → TradeProposal →
RiskEngine → OrderRequest → OrderReceipt → BrokerAdapter → Execution →
Portfolio. 星澄 emits signals/proposals only; risk limits and broker
adapters are unreachable from the AI path.
"""

from .contracts import (
    Account,
    Broker,
    CashBalance,
    Execution,
    FundDetails,
    Instrument,
    InstrumentType,
    MarketObservation,
    OrderReceipt,
    OrderRequest,
    OrderSide,
    OrderStatus,
    PortfolioSnapshot,
    Position,
    RiskDecision,
    TradeProposal,
    TradingMode,
    TradingSignal,
)
from .domains import DOMAINS
from .engine_service import TradingEngineService

__all__ = (
    "Account",
    "Broker",
    "CashBalance",
    "DOMAINS",
    "Execution",
    "FundDetails",
    "Instrument",
    "InstrumentType",
    "MarketObservation",
    "OrderReceipt",
    "OrderRequest",
    "OrderSide",
    "OrderStatus",
    "PortfolioSnapshot",
    "Position",
    "RiskDecision",
    "TradeProposal",
    "TradingEngineService",
    "TradingMode",
    "TradingSignal",
)
