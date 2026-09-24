"""Investment Mobile — 星澄 AI 投資管理與自動操盤系統 tool boundary.

Hosts the governed trading engine cluster (risk / strategy / OMS /
portfolio / broker adapters / trading audit) organised as 11 logical
domains. Business data and AI analysis remain owned by ai-assistant
behind the submit-only channel.
"""

from __future__ import annotations

from .integration import ChannelClient, ExternalAPIClient
from .trading import (
    DOMAINS,
    Execution,
    Instrument,
    OrderRequest,
    PortfolioSnapshot,
    Position,
    RiskDecision,
    TradeProposal,
    TradingEngineService,
    TradingMode,
    TradingSignal,
)

__version__ = "1.0.0"

__all__ = [
    "ChannelClient",
    "DOMAINS",
    "Execution",
    "ExternalAPIClient",
    "Instrument",
    "OrderRequest",
    "PortfolioSnapshot",
    "Position",
    "RiskDecision",
    "TradeProposal",
    "TradingEngineService",
    "TradingMode",
    "TradingSignal",
]
