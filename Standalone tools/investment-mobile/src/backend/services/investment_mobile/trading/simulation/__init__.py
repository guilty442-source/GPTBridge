"""Simulation layer — SHADOW signals + PAPER virtual trading."""

from .accounts import PaperAccountService
from .contracts import (
    CashState, PaperAccount, PaperExecution, PaperOrder,
    PaperOrderStatus, PaperPosition, ShadowSignal, StrategyRun,
)
from .engine import SimulationTradingEngine
from .execution import PaperExecutionEngine
from .loop import PaperStrategyCoordinator, StrategyExecutionLoop
from .orders import PaperOrderManagementSystem
from .performance import (
    PaperPerformanceService, ShadowPaperComparison,
    StrategyBenchmarkService,
)
from .positions import PaperPositionService
from .recovery import SimulationRecoveryService
from .risk import PaperRiskEngine
from .shadow import ShadowTradingService, SignalOutcomeTracker

__all__ = [
    "CashState", "PaperAccount", "PaperAccountService",
    "PaperExecution", "PaperExecutionEngine", "PaperOrder",
    "PaperOrderManagementSystem", "PaperOrderStatus",
    "PaperPerformanceService", "PaperPosition", "PaperPositionService",
    "PaperRiskEngine", "PaperStrategyCoordinator",
    "ShadowPaperComparison", "ShadowSignal", "ShadowTradingService",
    "SignalOutcomeTracker", "SimulationRecoveryService",
    "SimulationTradingEngine", "StrategyBenchmarkService",
    "StrategyExecutionLoop", "StrategyRun",
]
