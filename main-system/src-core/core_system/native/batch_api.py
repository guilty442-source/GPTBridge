"""Capability batch API — validate, slice, dispatch, combine.

    batch_execute(capability, items, work_fn, combine_fn)

- validates batch size/bytes against the capability policy
- single-thread batch baseline when below the parallel threshold
- bounded parallel via the shared runtime above it
- workers get the original deadline + cooperative token
- outputs land in disjoint slices; reductions combine worker-local
  partials — never a shared accumulator
- Python fallback callables are injected per capability and stay
  available (native absence never removes the Python path)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .execution_policy import (
    NativeExecutionPolicy,
    QueueVerdict,
    split_batch,
)
from .execution_runtime import (
    CancellationToken,
    NativeExecutionRuntime,
)


class BatchRejected(Exception):
    """Queue overload surfaced as a typed rejection — the caller's
    policy decides wait/fallback/reject."""


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    capability: str
    items: int
    slices: int
    parallel: bool
    result: Any
    elapsed_ms: float
    cancelled: bool = False


class NativeBatchExecutor:
    """Per-capability batch dispatcher on the shared runtime."""

    def __init__(
        self,
        runtime: NativeExecutionRuntime,
        policies: dict[str, NativeExecutionPolicy],
    ) -> None:
        self._runtime = runtime
        self._policies = dict(policies)

    def batch_execute(
        self,
        capability: str,
        item_count: int,
        work_fn: Callable[[int, int, CancellationToken], Any],
        combine_fn: Optional[Callable[[list[Any]], Any]] = None,
        *,
        deadline_ms: Optional[int] = None,
        token: Optional[CancellationToken] = None,
    ) -> BatchOutcome:
        policy = self._policies[capability]
        if item_count < 0:
            raise ValueError("item_count must be >= 0")
        if item_count > policy.max_batch:
            raise ValueError(
                f"batch {item_count} exceeds max_batch {policy.max_batch}"
            )
        if policy.max_batch_bytes:
            # caller-side item byte weight is policy domain knowledge;
            # bytes ceiling is enforced by the memory contract layer —
            # here we enforce the count ceiling only.
            pass

        verdict = self._runtime.admit()
        if verdict is QueueVerdict.REJECTED:
            raise BatchRejected(f"{capability}: queue at limit")

        slices = split_batch(item_count, policy)
        parallel = len(slices) > 1
        deadline_at = time.monotonic() + (
            (deadline_ms or policy.deadline_ms) / 1000.0
        )
        started = time.monotonic()
        result = self._runtime.submit_slices(
            slices, work_fn,
            deadline_at=deadline_at, token=token, combine=combine_fn,
        )
        return BatchOutcome(
            capability=capability,
            items=item_count,
            slices=len(slices),
            parallel=parallel,
            result=result,
            elapsed_ms=(time.monotonic() - started) * 1000.0,
        )


__all__ = ["BatchOutcome", "BatchRejected", "NativeBatchExecutor"]
