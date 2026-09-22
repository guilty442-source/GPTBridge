"""Adaptive data-plane facade.

One object composes every controller and exposes the call-site API:

    plane = get_plane()
    plane.observe(signals)                    # once per sampling cycle
    decision = plane.admit("transport", signals)
    retry = plane.retry_for(error, attempt=1)
    plane.guard("transport")                  # per-domain breaker
    batch, delay, reason = plane.plan_upserts(...)

The plane is opt-in: an unconfigured plane only records metrics and answers
with ``ALLOW``-style defaults, so wiring it never changes behaviour until a
call site is explicitly updated to consult it.
"""

from __future__ import annotations

import threading
from typing import Final, Iterable

from .admission import AdmissionController, ModuleUsage, class_for_workload
from .breakers import DomainBreakerRegistry
from .budgets import QdrantIndexingBudget, SqliteFallbackBudget
from .cost_gate import QueryCostGate
from .maintenance import MaintenanceScheduler
from .retry_policy import AdaptiveRetryPolicy, RetryDecision
from .tuner import BoundedAdaptiveTuner
from .types import (
    AdaptiveEnvelope,
    Decision,
    DecisionKind,
    LoadSignals,
    PriorityClass,
    ResourceBudget,
)


class AdaptiveDataPlane:
    def __init__(
        self,
        envelope: AdaptiveEnvelope | None = None,
        *,
        admission: AdmissionController | None = None,
        tuner: BoundedAdaptiveTuner | None = None,
        breakers: DomainBreakerRegistry | None = None,
        retry_policy: AdaptiveRetryPolicy | None = None,
        cost_gate: QueryCostGate | None = None,
        maintenance: MaintenanceScheduler | None = None,
        sqlite_budget: SqliteFallbackBudget | None = None,
        qdrant_budget: QdrantIndexingBudget | None = None,
    ) -> None:
        self.envelope = envelope or AdaptiveEnvelope()
        self.admission = admission or AdmissionController()
        self.tuner = tuner or BoundedAdaptiveTuner(self.envelope)
        self.breakers = breakers or DomainBreakerRegistry()
        self.retry_policy = retry_policy or AdaptiveRetryPolicy()
        self.cost_gate = cost_gate or QueryCostGate(self.envelope)
        self.maintenance = maintenance or MaintenanceScheduler(envelope=self.envelope)
        self.sqlite_budget = sqlite_budget or SqliteFallbackBudget()
        self.qdrant_budget = qdrant_budget or QdrantIndexingBudget()
        self._lock = threading.RLock()
        self._signals = LoadSignals()
        self._counters: dict[str, int] = {}
        self._last_parameters: dict[str, int | float] = self.tuner.parameters()

    # -- observations ------------------------------------------------------

    def observe(self, signals: LoadSignals) -> dict[str, int | float]:
        with self._lock:
            self._signals = signals
            self._last_parameters = self.tuner.observe(signals)
            return dict(self._last_parameters)

    def observe_merge(
        self, signals: LoadSignals, *, fields: Iterable[str]
    ) -> dict[str, int | float]:
        """欄位級合併觀測——多生產者支援（P4 adaptive plane 殘項）。

        ``observe()`` 整體替換會抹掉其他生產者的量測；各生產者擁有
        不相交的欄位集合，只覆寫 ``fields`` 內的欄位，其餘保留上次觀測值
        （初次觀測時未覆寫欄位 = 預設「無壓力」，語意正確）。
        未知欄位名略過（fail-closed：不憑空生欄位）。
        """
        with self._lock:
            merged = self._signals
            for name in fields:
                if hasattr(merged, name) and hasattr(signals, name):
                    setattr(merged, name, getattr(signals, name))
            self._signals = merged
            self._last_parameters = self.tuner.observe(merged)
            return dict(self._last_parameters)

    @property
    def signals(self) -> LoadSignals:
        return self._signals

    def parameters(self) -> dict[str, int | float]:
        with self._lock:
            return dict(self._last_parameters)

    def register_module(self, budget: ResourceBudget) -> None:
        self.admission.register(budget)

    def update_usage(self, usage: ModuleUsage) -> None:
        self.admission.update_usage(usage)

    # -- decisions ---------------------------------------------------------

    def admit(
        self,
        workload: str,
        signals: LoadSignals | None = None,
        *,
        module_id: str | None = None,
        budget: ResourceBudget | None = None,
        priority_class: PriorityClass | None = None,
    ) -> Decision:
        decision = self.admission.evaluate(
            workload,
            signals if signals is not None else self._signals,
            module_id=module_id,
            budget=budget,
            priority_class=priority_class,
        )
        self._count("admit:" + decision.kind.value)
        if decision.kind is not DecisionKind.ALLOW:
            self._count("admit:" + workload + ":" + decision.kind.value)
        return decision

    def admit_write(
        self,
        workload: str,
        priority_class: PriorityClass,
        signals: LoadSignals | None = None,
    ) -> Decision:
        current = signals if signals is not None else self._signals
        if current.degraded:
            decision = self.sqlite_budget.accept_write(current, priority_class)
            self._count("degraded-write:" + decision.kind.value)
            return decision
        return self.admit(workload, current, priority_class=priority_class)

    def retry_for(self, error: BaseException, attempt: int) -> RetryDecision:
        decision = self.retry_policy.decide(error, attempt)
        self._count("retry:" + decision.kind.value)
        return decision

    def guard(self, domain: str) -> None:
        self.breakers.guard(domain)

    def record_result(self, domain: str, ok: bool) -> None:
        if ok:
            self.breakers.record_success(domain)
        else:
            self.breakers.record_failure(domain)

    def plan_upserts(
        self,
        *,
        metadata_pending: int,
        pending_points: int,
        signals: LoadSignals | None = None,
    ) -> tuple[int, float, str]:
        current = signals if signals is not None else self._signals
        plan = self.qdrant_budget.plan_upserts(
            current,
            metadata_pending=metadata_pending,
            pending_points=pending_points,
            envelope=self.envelope,
            configured_rate=self.tuner.upsert_rate_per_second(),
        )
        self._count("upserts-planned" if plan[0] else "upserts-paused")
        return plan

    def assess_cost(self, cost, *, workload: str = "interactive", **kwargs) -> Decision:
        decision = self.cost_gate.assess(cost, workload=workload, priority_class=class_for_workload(workload), **kwargs)
        self._count("cost:" + decision.kind.value)
        return decision

    def next_maintenance(self):
        return self.maintenance.next_task(self._signals)

    # -- observability -----------------------------------------------------

    def _count(self, key: str) -> None:
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + 1

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "parameters": dict(self._last_parameters),
                "pressure": self._signals.pressure().value,
                "degraded": self._signals.degraded,
                "counters": dict(self._counters),
                "breakers": self.breakers.snapshot(),
                "maintenance": self.maintenance.snapshot(),
            }


_PLANE: Final[AdaptiveDataPlane] = AdaptiveDataPlane()


def get_plane() -> AdaptiveDataPlane:
    """Process-wide adaptive plane (opt-in; safe before any observation)."""
    return _PLANE


__all__ = ["AdaptiveDataPlane", "get_plane"]
