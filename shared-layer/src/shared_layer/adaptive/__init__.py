"""Adaptive SQL-layer control.

Bounded, pre-approved automation for the local data platform:

  * admission control with priority classes and a load-shedding ladder,
  * dynamic connection-pool size, batch size and reconcile workers,
  * per-error adaptive retry policy,
  * per-domain circuit breakers (transport/index/audit/reconcile/rag),
  * idle-aware maintenance scheduling,
  * query cost gate and vector indexing budgets.

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
from .bounded_executor import (
    AdmissionRejected,
    BoundedExecutor,
    OverflowPolicy,
    PoolPaused,
    PoolPolicy,
    WorkExpired,
    pool_for,
)
from .bounded_executor_async import AsyncBoundedExecutor
from .breakers import DOMAINS, DomainBreakerRegistry
from .budget_source import ClassQuota, class_quota, read_budget
from .budgets import VectorIndexingBudget
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
    "AdmissionRejected",
    "AsyncBoundedExecutor",
    "BoundedExecutor",
    "BoundedAdaptiveTuner",
    "ClassQuota",
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
    "OverflowPolicy",
    "PRIORITY_CLASS_ORDER",
    "PoolPaused",
    "PoolPolicy",
    "PROTECTED_WORKLOADS",
    "PressureLevel",
    "PriorityClass",
    "QueryCost",
    "QueryCostGate",
    "ResourceBudget",
    "RetryDecision",
    "RetryKind",
    "SHED_LADDER",
    "VectorIndexingBudget",
    "WorkExpired",
    "class_for_workload",
    "class_quota",
    "get_plane",
    "pool_for",
    "read_budget",
]
