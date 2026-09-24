"""PerformanceRuntime — phase-13 facade composing the runtime services.

Wraps existing engines (no second authority): budget, metrics, cache,
subscriptions, incremental indicators, strategy pool, inference
scheduler, job manager, maintenance, recovery, power state, lifecycle,
retention, health. All offline; SHADOW/PAPER boundaries unchanged.
"""
from __future__ import annotations

from typing import Any

from .budget import InvestmentResourceBudget
from .cache import InvestmentCachePolicy
from .health import InvestmentHealthView
from .incremental import IncrementalIndicatorState
from .inference import InvestmentInferenceScheduler
from .jobs import InvestmentJobManager
from .lifecycle import InvestmentLifecycleController
from .maintenance import InvestmentMaintenanceService
from .metrics import InvestmentRuntimeMetrics
from .pool import StrategyExecutionPool
from .power import WindowsPowerStateHandler
from .recovery import InvestmentRecoveryCoordinator
from .retention import InvestmentRetentionService
from .subscriptions import MarketDataSubscriptionManager


class PerformanceRuntime:
    def __init__(self, *, autotrade: Any = None, metrics: Any = None
                 ) -> None:
        self.metrics = metrics or InvestmentRuntimeMetrics()
        self.budget = InvestmentResourceBudget()
        self.cache = InvestmentCachePolicy(
            max_entries=self.budget.limit("cache_entries"))
        self.subscriptions = MarketDataSubscriptionManager()
        self.jobs = InvestmentJobManager(
            max_jobs=self.budget.limit("background_jobs"))
        self.pool = StrategyExecutionPool(
            self.budget, max_concurrent=self.budget.limit("cpu_workers"))
        self.inference = InvestmentInferenceScheduler(
            getattr(autotrade, "workload", None), self.budget)
        self.lifecycle = InvestmentLifecycleController()
        self.power = WindowsPowerStateHandler()
        self.maintenance = InvestmentMaintenanceService()
        self.recovery = InvestmentRecoveryCoordinator()
        self.retention = InvestmentRetentionService(purgers={
            "finished_jobs": lambda: self.jobs.reap(),
        })
        self.indicators: dict[str, IncrementalIndicatorState] = {}
        self._autotrade = autotrade

    # --------------------------------------------------------------
    def indicator(self, instrument_id: str) -> IncrementalIndicatorState:
        return self.indicators.setdefault(
            str(instrument_id), IncrementalIndicatorState())

    def health(self) -> dict[str, Any]:
        return InvestmentHealthView(probes={
            "lifecycle": self.lifecycle.status,
            "budget": self.budget.status,
            "jobs": self.jobs.status,
            "inference": self.inference.health,
            "metrics": self.metrics.snapshot,
            "subscriptions": self.subscriptions.status,
            "power": self.power.status,
            "autotrade": (self._autotrade.overview
                          if self._autotrade is not None
                          else lambda: {"ok": True, "note": "not wired"}),
        }).health()

    def overview(self) -> dict[str, Any]:
        return {
            "ok": True,
            "lifecycle": self.lifecycle.state,
            "budget": self.budget.status(),
            "jobs": self.jobs.status(),
            "inference": self.inference.health(),
            "subscriptions": self.subscriptions.status(),
            "cache": self.cache.stats(),
            "metrics": self.metrics.snapshot(),
            "indicators": len(self.indicators),
            "note": "SHADOW/PAPER only — LIVE stays phase-locked; "
                    "所有維護為確定性，AI 僅分析",
        }
