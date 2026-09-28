"""§10.30 / A590 unified thread-budget entry point.

Single authority for the fixed five-core parallel budget:
``threads_per_worker × parallel_workers ≤ core_budget``.  The budget is
an application-level concurrency limit only — it may be lowered, never
raised above five cores, and never touches machine settings (no CPU
affinity, power plan or system configuration changes).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# A590: fixed five-core budget — one per core engine; lower-only.
CORE_BUDGET_CAP = 5

# A590/A593: work classes published by the C++23 resource-governor's
# ``concurrency-budget/v1`` state section.  Executors may pass ``workload``
# to the bounded_* helpers so a pool is sized by the governor's live
# per-class quota instead of only the static envelope; TRAINING/BATCH/
# MAINTENANCE shed first under pressure (A598 shed order).
WORKLOAD_CLASSES: tuple[str, ...] = (
    "interactive",
    "model",
    "rag",
    "network",
    "batch",
    "maintenance",
    "training",
    "verification",
)

_GOVERNOR_STATE = (
    Path(__file__).resolve().parents[4]
    / "main-system"
    / "runtime"
    / "state"
    / "resource-governor.json"
)


def logical_cores() -> int:
    """Logical processors visible to this process (floor 1)."""
    return max(1, os.cpu_count() or 1)


def workload_quota(
    workload: str, *, state_path: Optional[Path] = None
) -> Optional[int]:
    """Governor-published quota for ``workload`` or ``None`` when unusable.

    Fail-open: missing/stale/disabled state, a kill-switch-disabled
    governor, or an unknown class all yield ``None`` so the caller falls
    back to the static A590 envelope — a dead governor must never
    deadlock the fleet (same contract as
    ``tasks/resource_governor_signal.py``).  ``0`` means the class is
    paused under the governor's shed order.
    """
    if not workload:
        return None
    try:
        # Lazy import: keeps ``shared_layer.performance`` import-cheap and
        # keeps this module usable even if the adaptive package is absent.
        from shared_layer.adaptive.budget_source import class_quota
    except Exception:
        return None
    try:
        quota = class_quota(str(workload), state_path or _GOVERNOR_STATE)
    except Exception:
        return None
    return None if quota is None else quota.quota


def workload_paused(
    workload: str, *, state_path: Optional[Path] = None
) -> bool:
    """True when the governor currently pauses ``workload`` (quota 0)."""
    quota = workload_quota(workload, state_path=state_path)
    return quota is not None and quota <= 0


def core_budget(*, override: Optional[int] = None) -> int:
    """Effective core budget: ``min(cap, logical cores)``.

    ``override`` may only *lower* the budget — values above the cap are
    clamped down, never raised (A590).
    """
    budget = min(CORE_BUDGET_CAP, logical_cores())
    if override is not None and int(override) > 0:
        budget = min(budget, int(override))
    return max(1, budget)


def bounded_workers(
    requested: int,
    *,
    budget: Optional[int] = None,
    workload: Optional[str] = None,
) -> int:
    """Clamp a worker-pool size into the core budget (never unbounded).

    ``workload`` selects a governor work class (``WORKLOAD_CLASSES``);
    when the governor publishes a live quota for it the effective limit
    is ``min(static budget, class quota)`` — never raised.  A paused
    class (quota 0) degrades to a single sequential worker; callers that
    can skip the work entirely should consult :func:`workload_paused`.
    """
    limit = core_budget() if budget is None else max(1, int(budget))
    if workload is not None:
        quota = workload_quota(workload)
        if quota is not None:
            limit = min(limit, max(1, quota))
    return max(1, min(int(requested), limit))


def bounded_threads(
    threads_per_worker: int,
    parallel_workers: int,
    *,
    budget: Optional[int] = None,
    workload: Optional[str] = None,
) -> int:
    """Clamp ``threads_per_worker`` so threads × workers ≤ budget.

    ``parallel_workers`` is itself clamped through :func:`bounded_workers`
    first, so the product invariant holds even when the requested worker
    count exceeds the budget on its own.  ``workload`` applies the
    governor's per-class quota to the combined budget before splitting.
    """
    limit = core_budget() if budget is None else max(1, int(budget))
    if workload is not None:
        quota = workload_quota(workload)
        if quota is not None:
            limit = min(limit, max(1, quota))
    workers = bounded_workers(parallel_workers, budget=limit)
    return max(1, min(int(threads_per_worker), max(1, limit // workers)))


# BLAS/OpenMP runtimes honour these env vars at process start; PyTorch
# intra-op threads follow torch.set_num_threads (applied by the model
# backend through the same entry).
THREAD_ENV_VARS: tuple[str, ...] = (
    "OMP_NUM_THREADS",
    "MKL_NUM_THREADS",
    "OPENBLAS_NUM_THREADS",
    "NUMEXPR_NUM_THREADS",
)


def thread_env(
    threads_per_worker: int,
    parallel_workers: int,
    *,
    budget: Optional[int] = None,
    workload: Optional[str] = None,
) -> dict[str, str]:
    """Env values for one bounded allocation (threads × workers ≤ budget).

    Returns a plain mapping — callers merge it into a subprocess env or
    apply it in-process via :func:`apply_thread_env`.
    """
    threads = bounded_threads(
        threads_per_worker, parallel_workers, budget=budget, workload=workload
    )
    return {name: str(threads) for name in THREAD_ENV_VARS}


def apply_thread_env(
    threads_per_worker: int,
    parallel_workers: int,
    *,
    budget: Optional[int] = None,
    workload: Optional[str] = None,
    environ: Optional[dict] = None,
) -> int:
    """Set OMP/BLAS thread env for this process; returns applied threads.

    Application-level only (process env vars) — never touches machine
    settings (A590: no affinity / power plan / registry changes).  An
    already-set variable is respected: explicit governor/operator values
    win over the computed budget.
    """
    env = environ if environ is not None else os.environ
    values = thread_env(
        threads_per_worker, parallel_workers, budget=budget, workload=workload
    )
    for name, value in values.items():
        env.setdefault(name, value)
    return int(next(iter(values.values())))


def allocation_within_budget(
    threads_per_worker: int,
    parallel_workers: int,
    budget_cores: int,
) -> bool:
    """A590 predicate: allocation must fit the budget and the budget
    itself may never exceed the five-core cap."""
    try:
        threads = int(threads_per_worker)
        workers = int(parallel_workers)
        budget = int(budget_cores)
    except (TypeError, ValueError):
        return False
    if threads < 1 or workers < 1 or budget < 1:
        return False
    if budget > min(CORE_BUDGET_CAP, logical_cores()):
        return False
    return threads * workers <= budget


__all__ = [
    "CORE_BUDGET_CAP",
    "THREAD_ENV_VARS",
    "WORKLOAD_CLASSES",
    "allocation_within_budget",
    "apply_thread_env",
    "bounded_threads",
    "bounded_workers",
    "core_budget",
    "logical_cores",
    "thread_env",
    "workload_paused",
    "workload_quota",
]
