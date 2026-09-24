"""Strategy layer — registration, versions, signals, validation."""

from .contracts import (
    MarketKind, ParamSearchRecord, StrategyDefinition, StrategyStatus,
    StrategyType, StrategyVersionSnapshot,
)
from .registry import StrategyRegistry, StrategyVersionService
from .research import AIStrategyResearchService
from .signals import SignalEvent, generate
from .validation import StrategyEvaluationService, StrategyValidationService

__all__ = [
    "AIStrategyResearchService", "MarketKind", "ParamSearchRecord",
    "SignalEvent", "StrategyDefinition", "StrategyEvaluationService",
    "StrategyRegistry", "StrategyStatus", "StrategyType",
    "StrategyValidationService", "StrategyVersionService",
    "StrategyVersionSnapshot", "generate",
]
