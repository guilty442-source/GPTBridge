"""Backtest layer — PIT-safe strategy replay across TW/US/fund."""

from .comparison import BacktestComparisonService
from .contracts import (
    BacktestConfig, BacktestResult, BacktestTrade, EquityPoint,
)
from .cost import FeeRule, TradingCostEngine
from .engine import BacktestEngine
from .execution import ExecutionSimulationEngine, FillResult
from .fund_engine import FundBacktestEngine
from .pit import (
    PITViolation, assert_no_future, gate_corporate, gate_nav,
    nav_available_at, slice_bars_at,
)
from .portfolio_engine import PortfolioBacktestEngine
from .queue import BacktestJobQueue, BacktestMaintenance
from .rules import (
    BrokerCapabilityProfile, CAPABILITY_PROFILES, MarketRules,
    TAIWAN_RULES, US_RULES, capability_for, rules_for,
)
from .universe import HistoricalUniverseService

__all__ = [
    "BacktestComparisonService", "BacktestConfig", "BacktestEngine",
    "BacktestJobQueue", "BacktestMaintenance", "BacktestResult",
    "BacktestTrade", "BrokerCapabilityProfile", "CAPABILITY_PROFILES",
    "EquityPoint", "ExecutionSimulationEngine", "FeeRule", "FillResult",
    "FundBacktestEngine", "HistoricalUniverseService", "MarketRules",
    "PITViolation", "PortfolioBacktestEngine", "TAIWAN_RULES",
    "TradingCostEngine", "US_RULES", "assert_no_future",
    "capability_for", "gate_corporate", "gate_nav", "nav_available_at",
    "rules_for", "slice_bars_at",
]
