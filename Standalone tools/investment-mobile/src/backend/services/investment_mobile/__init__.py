"""Investment Mobile — 星澄 AI 投資管理與自動操盤系統 tool boundary.

Hosts the governed trading engine cluster (risk / strategy / OMS /
portfolio / broker adapters / trading audit). Business data and AI
analysis remain owned by ai-assistant behind the submit-only channel.
"""

from __future__ import annotations

from .integration import ChannelClient, ExternalAPIClient
from .trading import (
    Fill,
    Order,
    OrderIntent,
    Position,
    RiskDecision,
    Signal,
    TradingEngineService,
    TradingMode,
)

__version__ = "1.0.0"

__all__ = [
    "ChannelClient",
    "ExternalAPIClient",
    "Fill",
    "Order",
    "OrderIntent",
    "Position",
    "RiskDecision",
    "Signal",
    "TradingEngineService",
    "TradingMode",
]
