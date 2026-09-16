"""NativeExecutionRuntime — the single shared bounded pool.

Parser, vector and transform all dispatch through this runtime; no
domain builds its own thread pool.  Guarantees:

    - bounded workers (fixed at construction, benchmark-derived)
    - bounded queue -> typed QueueVerdict backpressure to Python
    - no nested parallelism (workers cannot submit)
    - no per-request threads, no unbounded queue
    - original request deadline propagated to every worker
    - cooperative cancellation checked between chunks
    - worker-local partials + final combine (no shared accumulator)

C++ never sees this layer and never makes degradation decisions —
overload surfaces as a typed status and the Python policy decides.
"""
from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .execution_policy import NativeExecutionPolicy, QueueVerdict


class Cancelled(Exception):
    """Cooperative cancellation tripped between chunks."""


class DeadlineExceeded(Exception):
    """Original request deadline elapsed."""


class CancellationToken:
    """Cooperative token — workers poll between chunks."""

    __slots__ = ("_event",)

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()


@dataclass(slots=True)
class RuntimeStats:
    submitted: int = 0
    completed: int = 0
    cancelled: int = 0
    rejected: int = 0
    deadline_exceeded: int = 0
    queue_depth: int = 0
    peak_queue_depth: int = 0


class NativeExecutionRuntime:
    """Shared bounded runtime for all approved native capabilities."""

    def __init__(self, *, max_workers: int, queue_limit: int) -> None:
        if max_workers < 1:
            raise ValueError("max_workers must be >= 1")
        if queue_limit < 1:
            raise ValueError("queue_limit must be >= 1")
        self._max_workers = max_workers
        self._queue_limit = queue_limit
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="gb-native"
        )
        self._in_flight = threading.Semaphore(queue_limit)
        self._stats = RuntimeStats()
        self._stats_lock = threading.Lock()
        self._worker_ctx = threading.local()
        self._closed = False

    # ------------------------------------------------------------------
    def _record(self, **kw: int) -> None:
        with self._stats_lock:
            for k, v in kw.items():
                setattr(self._stats, k, getattr(self._stats, k) + v)
            if self._stats.queue_depth > self._stats.peak_queue_depth:
                self._stats.peak_queue_depth = self._stats.queue_depth

    @property
    def stats(self) -> RuntimeStats:
        with self._stats_lock:
            import dataclasses
            return dataclasses.replace(self._stats)

    @property
    def queue_depth(self) -> int:
        return self.stats.queue_depth

    # ------------------------------------------------------------------
    def admit(self) -> QueueVerdict:
        """Typed admission — the caller's policy decides what to do
        with BACKPRESSURE/REJECTED; the runtime never degrades work."""
        with self._stats_lock:
            depth = self._stats.queue_depth
        if depth >= self._queue_limit:
            self._record(rejected=1)
            return QueueVerdict.REJECTED
        if depth >= int(self._queue_limit * 0.8):
            return QueueVerdict.BACKPRESSURE
        return QueueVerdict.ACCEPTED

    def submit_slices(
        self,
        slices: list[tuple[int, int]],
        worker: Callable[[int, int, CancellationToken], Any],
        *,
        deadline_at: float,
        token: Optional[CancellationToken] = None,
        combine: Optional[Callable[[list[Any]], Any]] = None,
    ) -> Any:
        """Dispatch disjoint slices; workers receive the ORIGINAL
        request deadline and a cooperative token.  Results are
        worker-local partials combined by ``combine`` — no shared
        mutable accumulator."""
        if getattr(self._worker_ctx, "active", False):
            raise RuntimeError("nested parallelism is forbidden")
        if self._closed:
            raise RuntimeError("runtime is shut down")

        verdict = self.admit()
        if verdict is QueueVerdict.REJECTED:
            raise RuntimeError("queue full — caller must wait/fallback/reject")

        token = token or CancellationToken()
        n = len(slices)
        if n == 0:
            return combine([]) if combine else []
        if not self._in_flight.acquire(blocking=False):
            self._record(rejected=1)
            raise RuntimeError("queue full — caller must wait/fallback/reject")

        self._record(submitted=1, queue_depth=1)
        try:
            def _run(s: int, e: int) -> Any:
                self._worker_ctx.active = True
                try:
                    if time.monotonic() > deadline_at:
                        raise DeadlineExceeded()
                    token.check()
                    return worker(s, e, token)
                finally:
                    self._worker_ctx.active = False

            if n == 1 or self._max_workers == 1:
                # single-thread batch baseline — no pool round-trip
                partials = [_run(s, e) for s, e in slices]
            else:
                futures = [
                    self._executor.submit(_run, s, e) for s, e in slices
                ]
                partials = []
                try:
                    for f in futures:
                        partials.append(f.result())
                except Cancelled:
                    self._record(cancelled=1)
                    raise
                except DeadlineExceeded:
                    self._record(deadline_exceeded=1)
                    raise
            self._record(completed=1)
            return combine(partials) if combine else partials
        finally:
            self._in_flight.release()
            self._record(queue_depth=-1)

    def shutdown(self, *, wait: bool = True) -> None:
        self._closed = True
        self._executor.shutdown(wait=wait)


__all__ = [
    "Cancelled",
    "CancellationToken",
    "DeadlineExceeded",
    "NativeExecutionRuntime",
    "RuntimeStats",
]
