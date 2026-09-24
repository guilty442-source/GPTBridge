"""星澄 AI 投資管理與自動操盤系統 — trading engine package.

Decision-free governed engines physically hosted inside the
``investment-mobile`` tool boundary:

- ``modes``            : ANALYSIS / SHADOW / PAPER / LIVE gate (default ANALYSIS)
- ``risk_engine``      : C-core façade — fail-closed limit evaluation
- ``strategy_engine``  : C++-backed strategy evaluation surface
- ``oms``              : C#-backed order management state machine
- ``portfolio_engine`` : position / asset aggregation
- ``broker``           : independent broker adapters (analysis-only until the
                         official trading API is verified)
- ``audit``            : append-only trading audit journal (runtime mirror;
                         authoritative records live in ai-assistant)
"""

from .contracts import (
    Fill,
    Order,
    OrderIntent,
    Position,
    RiskDecision,
    Signal,
    TradingMode,
)
from .engine_service import TradingEngineService

__all__ = (
    "Fill",
    "Order",
    "OrderIntent",
    "Position",
    "RiskDecision",
    "Signal",
    "TradingEngineService",
    "TradingMode",
)
