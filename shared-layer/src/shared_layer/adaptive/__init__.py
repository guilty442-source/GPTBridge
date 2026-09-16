"""Adaptive SQL-layer control.

Bounded, pre-approved automation for the local data platform:

  * admission control with priority classes and a load-shedding ladder,
  * dynamic connection-pool size, batch size and reconcile workers,
  * per-error adaptive retry policy,
  * per-domain circuit breakers (transport/index/audit/reconcile/rag),
  * idle-aware maintenance scheduling,
  * query cost gate, SQLite fallback and Qdrant indexing budgets.

All adaptive parameters stay inside :class:`AdaptiveEnvelope`; nothing here
may widen its own limits.
"""

from .admission import (
    AdmissionController,
    ModuleUsage,
    PROTECTED_WORKLOADS,
    SHED_LADDER,
    class_for_workload,
)
from .breakers import DOMAINS, DomainBreakerRegistry
from .budgets import QdrantIndexingBudget, SqliteFallbackBudget
from .cost_gate import QueryCost, QueryCostGate
from .maintenance import DEFAULT_TASKS, MaintenanceScheduler, MaintenanceTask
from .plane import AdaptiveDataPlane, get_plane
from .retry_policy import AdaptiveRetryPolicy, RetryDecision, RetryKind
from .tuner import BoundedAdaptiveTuner
from .types import (
    ALLOW,
    DEFAULT_ENVELOPE,
    PRIORITY_CLASS_ORDER,
    AdaptiveEnvelope,
    Decision,
    DecisionKind,
    LoadSignals,
    PressureLevel,
    PriorityClass,
    ResourceBudget,
)

__all__ = [
    "ALLOW",
    "AdaptiveDataPlane",
    "AdaptiveEnvelope",
    "AdaptiveRetryPolicy",
    "AdmissionController",
    "BoundedAdaptiveTuner",
    "DEFAULT_ENVELOPE",
    "DEFAULT_TASKS",
    "DOMAINS",
    "Decision",
    "DecisionKind",
    "DomainBreakerRegistry",
    "LoadSignals",
    "MaintenanceScheduler",
    "MaintenanceTask",
    "ModuleUsage",
    "PRIORITY_CLASS_ORDER",
    "PROTECTED_WORKLOADS",
    "PressureLevel",
    "PriorityClass",
    "QdrantIndexingBudget",
    "QueryCost",
    "QueryCostGate",
    "ResourceBudget",
    "RetryDecision",
    "RetryKind",
    "SHED_LADDER",
    "SqliteFallbackBudget",
    "class_for_workload",
    "get_plane",
]
