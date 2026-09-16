"""Admission control, resource budgets and the load-shedding ladder.

Shedding order (least important first):

    optional analytics
      -> background indexing
        -> maintenance
          -> non-critical reconcile

Never shed: audit writes, governance-required writes, critical transport.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Final

from .types import (
    Decision,
    DecisionKind,
    LoadSignals,
    PressureLevel,
    PriorityClass,
    ResourceBudget,
)

# Shed ladder in the order traffic is sacrificed first.
SHED_LADDER: Final[tuple[str, ...]] = (
    "optional_analytics",
    "background_indexing",
    "maintenance",
    "non_critical_reconcile",
)

# Classes whose traffic is never shed by the ladder.
PROTECTED_CLASSES: Final[frozenset[PriorityClass]] = frozenset(
    {PriorityClass.CRITICAL, PriorityClass.INTERACTIVE}
)

# Workloads that cannot be shed regardless of class (governance requirement).
PROTECTED_WORKLOADS: Final[frozenset[str]] = frozenset(
    {"audit", "governance_write", "critical_transport"}
)

_WORKLOAD_CLASS: Final[dict[str, PriorityClass]] = {
    "critical_transport": PriorityClass.CRITICAL,
    "audit": PriorityClass.CRITICAL,
    "governance_write": PriorityClass.CRITICAL,
    "transport": PriorityClass.INTERACTIVE,
    "index": PriorityClass.INTERACTIVE,
    "interactive": PriorityClass.INTERACTIVE,
    "rag_metadata": PriorityClass.BACKGROUND,
    "background_indexing": PriorityClass.BACKGROUND,
    "optional_analytics": PriorityClass.BACKGROUND,
    "reconcile": PriorityClass.BACKGROUND,
    "maintenance": PriorityClass.MAINTENANCE,
}


def class_for_workload(workload: str, default: PriorityClass = PriorityClass.INTERACTIVE) -> PriorityClass:
    return _WORKLOAD_CLASS.get(workload, default)


class _RateWindow:
    """Sliding one-second window used for write/reconcile rate budgets."""

    def __init__(self) -> None:
        self._events: list[float] = []

    def allow(self, rate_per_second: float, now: float) -> bool:
        if rate_per_second <= 0:
            return False
        cutoff = now - 1.0
        self._events = [stamp for stamp in self._events if stamp > cutoff]
        if len(self._events) >= rate_per_second:
            return False
        self._events.append(now)
        return True

    def current(self, now: float) -> int:
        cutoff = now - 1.0
        self._events = [stamp for stamp in self._events if stamp > cutoff]
        return len(self._events)


@dataclass
class ModuleUsage:
    """Live counters for one module, compared against its budget."""

    module_id: str
    in_flight_queries: int = 0
    pending_requests: int = 0
    open_connections: int = 0
    reconcile_in_flight: int = 0


@dataclass
class _ModuleState:
    usage: ModuleUsage
    write_window: _RateWindow = field(default_factory=_RateWindow)
    reconcile_window: _RateWindow = field(default_factory=_RateWindow)


class AdmissionController:
    """Decides whether a request may run now, be deferred, or be rejected."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._modules: dict[str, _ModuleState] = {}

    def register(self, budget: ResourceBudget) -> None:
        with self._lock:
            self._modules[budget.module_id] = _ModuleState(
                usage=ModuleUsage(module_id=budget.module_id)
            )

    def update_usage(self, usage: ModuleUsage) -> None:
        with self._lock:
            state = self._modules.get(usage.module_id)
            if state is None:
                self._modules[usage.module_id] = _ModuleState(usage=usage)
            else:
                state.usage = usage

    def usage(self, module_id: str) -> ModuleUsage | None:
        with self._lock:
            state = self._modules.get(module_id)
            return state.usage if state else None

    def evaluate(
        self,
        workload: str,
        signals: LoadSignals,
        *,
        module_id: str | None = None,
        budget: ResourceBudget | None = None,
        priority_class: PriorityClass | None = None,
    ) -> Decision:
        cls = priority_class or class_for_workload(workload)
        pressure = signals.pressure()

        if workload in PROTECTED_WORKLOADS:
            return Decision(DecisionKind.ALLOW, f"protected:{workload}")
        if cls is PriorityClass.CRITICAL:
            return Decision(DecisionKind.ALLOW, "critical-class")

        if budget is not None and module_id is not None:
            budget_decision = self._check_budget(
                workload, module_id, budget, cls, signals
            )
            if budget_decision is not None:
                return budget_decision

        if reason := self._shed_reason(workload, cls, pressure):
            return Decision(
                DecisionKind.DEFER,
                reason,
                retry_after_seconds=_defer_seconds(pressure),
            )
        return Decision(DecisionKind.ALLOW, f"pressure:{pressure.value}")

    def _shed_reason(
        self,
        workload: str,
        cls: PriorityClass,
        pressure: PressureLevel,
    ) -> str | None:
        if pressure is PressureLevel.LOW:
            return None
        if cls is PriorityClass.MAINTENANCE:
            return "shed-ladder:maintenance"
        if pressure is PressureLevel.MODERATE:
            if workload in ("optional_analytics",):
                return "shed-ladder:optional_analytics"
            return None
        if pressure is PressureLevel.HIGH:
            if workload in ("optional_analytics", "background_indexing"):
                return f"shed-ladder:{workload}"
            return None
        # CRITICAL pressure
        if workload in ("optional_analytics", "background_indexing", "maintenance"):
            return f"shed-ladder:{workload}"
        if workload == "reconcile" or cls is PriorityClass.BACKGROUND:
            return "shed-ladder:non_critical_reconcile"
        return None

    def _check_budget(
        self,
        workload: str,
        module_id: str,
        budget: ResourceBudget,
        cls: PriorityClass,
        signals: LoadSignals,
    ) -> Decision | None:
        with self._lock:
            state = self._modules.get(module_id)
            now = time.monotonic()
            if state is not None:
                usage = state.usage
                if usage.in_flight_queries >= budget.max_concurrent_queries:
                    if cls is PriorityClass.BACKGROUND or cls is PriorityClass.MAINTENANCE:
                        return Decision(DecisionKind.DEFER, "budget:max_concurrent_queries", 1.0)
                if usage.pending_requests >= budget.max_pending_requests:
                    return Decision(DecisionKind.DEFER, "budget:max_pending_requests", 2.0)
                if usage.open_connections >= budget.max_connections:
                    if cls is PriorityClass.BACKGROUND or cls is PriorityClass.MAINTENANCE:
                        return Decision(DecisionKind.DEFER, "budget:max_connections", 1.0)
                if workload in ("reconcile", "non_critical_reconcile"):
                    if not state.reconcile_window.allow(budget.max_reconcile_rate, now):
                        return Decision(DecisionKind.DEFER, "budget:max_reconcile_rate", 1.0)
                elif workload not in ("audit", "governance_write"):
                    if not state.write_window.allow(budget.max_write_rate, now):
                        return Decision(DecisionKind.DEFER, "budget:max_write_rate", 0.5)
        return None


def _defer_seconds(pressure: PressureLevel) -> float:
    if pressure is PressureLevel.CRITICAL:
        return 5.0
    if pressure is PressureLevel.HIGH:
        return 2.0
    return 0.5


__all__ = [
    "AdmissionController",
    "ModuleUsage",
    "PROTECTED_CLASSES",
    "PROTECTED_WORKLOADS",
    "SHED_LADDER",
    "class_for_workload",
]
