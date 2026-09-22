"""§10.30 / A590 unified thread-budget entry point.

Single authority for the fixed five-core parallel budget:
``threads_per_worker × parallel_workers ≤ core_budget``.  The budget is
an application-level concurrency limit only — it may be lowered, never
raised above five cores, and never touches machine settings (no CPU
affinity, power plan or system configuration changes).
"""

from __future__ import annotations

import os
from typing import Optional

# A590: fixed five-core budget — one per core engine; lower-only.
CORE_BUDGET_CAP = 5


def logical_cores() -> int:
    """Logical processors visible to this process (floor 1)."""
    return max(1, os.cpu_count() or 1)


def core_budget(*, override: Optional[int] = None) -> int:
    """Effective core budget: ``min(cap, logical cores)``.

    ``override`` may only *lower* the budget — values above the cap are
    clamped down, never raised (A590).
    """
    budget = min(CORE_BUDGET_CAP, logical_cores())
    if override is not None and int(override) > 0:
        budget = min(budget, int(override))
    return max(1, budget)


def bounded_workers(requested: int, *, budget: Optional[int] = None) -> int:
    """Clamp a worker-pool size into the core budget (never unbounded)."""
    limit = core_budget() if budget is None else max(1, int(budget))
    return max(1, min(int(requested), limit))


def bounded_threads(
    threads_per_worker: int,
    parallel_workers: int,
    *,
    budget: Optional[int] = None,
) -> int:
    """Clamp ``threads_per_worker`` so threads × workers ≤ budget.

    ``parallel_workers`` is itself clamped through :func:`bounded_workers`
    first, so the product invariant holds even when the requested worker
    count exceeds the budget on its own.
    """
    limit = core_budget() if budget is None else max(1, int(budget))
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
) -> dict[str, str]:
    """Env values for one bounded allocation (threads × workers ≤ budget).

    Returns a plain mapping — callers merge it into a subprocess env or
    apply it in-process via :func:`apply_thread_env`.
    """
    threads = bounded_threads(
        threads_per_worker, parallel_workers, budget=budget
    )
    return {name: str(threads) for name in THREAD_ENV_VARS}


def apply_thread_env(
    threads_per_worker: int,
    parallel_workers: int,
    *,
    budget: Optional[int] = None,
    environ: Optional[dict] = None,
) -> int:
    """Set OMP/BLAS thread env for this process; returns applied threads.

    Application-level only (process env vars) — never touches machine
    settings (A590: no affinity / power plan / registry changes).  An
    already-set variable is respected: explicit governor/operator values
    win over the computed budget.
    """
    env = environ if environ is not None else os.environ
    values = thread_env(threads_per_worker, parallel_workers, budget=budget)
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
    "allocation_within_budget",
    "apply_thread_env",
    "bounded_threads",
    "bounded_workers",
    "core_budget",
    "logical_cores",
    "thread_env",
]
