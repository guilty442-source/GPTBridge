"""Bounded work executor (``bounded-concurrency/v1``).

Canonical consumer-side primitive for the bounded-concurrency mandate:
work is never spawned per request; it is admitted into a bounded queue
drained by a governor-sized worker pool.

Queue contract (all six properties are mandatory):

* **capacity**     — pending items bounded by ``queue_capacity``;
* **priority**     — :class:`PriorityClass` ordering (lower rank first);
* **deadline**     — items carry an absolute expiry; expired items are
  shed at dequeue with ``WorkExpired`` (queue wait is hard-bounded;
  running work is cooperatively cancellable via ``cancel_event``);
* **backpressure** — :meth:`submit` may wait up to ``backpressure_wait_ms``
  for capacity before applying the overflow policy;
* **cancellation** — ``cancel_event`` plus ``Future.cancel`` retire
  queued work before it starts;
* **drop/reject**  — when full, ``overflow="reject"`` raises
  :class:`AdmissionRejected`, ``overflow="drop_oldest"`` evicts the
  lowest-priority pending item.

Sizing: the worker count is the governor's per-class quota
(``concurrency-budget/v1`` state section, read via
:mod:`budget_source`), refreshed on every queue operation and clamped
into the module-declared ``min_workers``/``max_workers`` envelope
(A116).  Missing/stale state falls back to the declared static maximum —
a dead governor never deadlocks the fleet (fail-open, same contract as
``resource_governor_signal``).  ``paused`` classes reject new admissions.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, TypeVar

from .budget_source import class_quota
from .types import PriorityClass

T = TypeVar("T")
_logger = logging.getLogger("gptbridge.bounded_executor")

_STATE_FILE = (
    Path(__file__).resolve().parents[4]
    / "main-system" / "runtime" / "state" / "resource-governor.json"
)


class OverflowPolicy(Enum):
    REJECT = "reject"
    DROP_OLDEST = "drop_oldest"


class AdmissionRejected(RuntimeError):
    """Queue is at capacity and the overflow policy rejected the work."""


class PoolPaused(RuntimeError):
    """The governor paused this work class (shed order under pressure)."""


class WorkExpired(RuntimeError):
    """The item exceeded its queue deadline before dispatch."""


@dataclass(frozen=True)
class PoolPolicy:
    """Module-declared envelope for one pool (A116)."""

    pool: str
    work_class: str = "interactive"
    min_workers: int = 1
    max_workers: int = 4
    queue_capacity: int = 64
    deadline_ms: int = 30_000
    overflow: OverflowPolicy = OverflowPolicy.REJECT
    backpressure_wait_ms: int = 5_000


@dataclass
class PoolMetrics:
    admitted: int = 0
    completed: int = 0
    rejected: int = 0
    dropped: int = 0
    expired: int = 0
    cancelled: int = 0
    pending: int = 0
    running: int = 0
    workers: int = 0


@dataclass
class _Item:
    priority: int
    seq: int
    deadline: float
    fn: Callable
    args: tuple
    kwargs: dict
    cancel: threading.Event | None
    future: Future

    def __lt__(self, other: "_Item") -> bool:
        return (self.priority, self.seq) < (other.priority, other.seq)


class BoundedExecutor:
    """Fixed-but-adaptive worker pool draining a bounded priority queue."""

    def __init__(
        self,
        policy: PoolPolicy,
        *,
        state_path: Path | None = None,
    ) -> None:
        if policy.min_workers < 0 or policy.max_workers < 1:
            raise ValueError("worker bounds invalid")
        if policy.queue_capacity < 1 or policy.deadline_ms < 1:
            raise ValueError("queue bounds invalid")
        self._policy = policy
        self._state_path = state_path or _STATE_FILE
        self._cond = threading.Condition()
        self._heap: list[_Item] = []
        self._seq = itertools.count()
        self._threads: set[threading.Thread] = set()
        self._running = 0
        self._closed = False
        self._metrics = PoolMetrics()
        self._quota: int | None = None
        self._paused = False
        self._budget_checked = 0.0

    # -- governor budget -------------------------------------------------

    def _refresh_budget(self) -> None:
        """Adapt the target worker count to the governor's class quota."""
        now = time.monotonic()
        if now - self._budget_checked < 1.0:
            return
        self._budget_checked = now
        quota = class_quota(self._policy.work_class, self._state_path)
        if quota is None:
            self._quota = None
            self._paused = False
            return
        self._paused = quota.paused
        self._quota = max(
            self._policy.min_workers,
            min(self._policy.max_workers, quota.quota),
        )

    def _target_workers(self) -> int:
        if self._quota is not None:
            return self._quota
        return self._policy.max_workers  # fail-open static envelope

    # -- admission --------------------------------------------------------

    def submit(
        self,
        fn: Callable[..., T],
        /,
        *args,
        priority: PriorityClass = PriorityClass.INTERACTIVE,
        deadline_ms: int | None = None,
        cancel: threading.Event | None = None,
        wait_ms: int = 0,
        **kwargs,
    ) -> Future:
        """Admit work; returns a Future. Never raises from the work itself."""
        future: Future = Future()
        with self._cond:
            self._refresh_budget()
            if self._closed:
                future.set_exception(AdmissionRejected("executor-closed"))
                return future
            if self._paused:
                self._metrics.rejected += 1
                future.set_exception(PoolPaused(self._policy.work_class))
                return future
            if cancel is not None and cancel.is_set():
                self._metrics.cancelled += 1
                future.cancel()
                return future
            if not self._admit_locked(future, wait_ms):
                return future
            item = _Item(
                priority=priority.rank,
                seq=next(self._seq),
                deadline=time.monotonic()
                + (deadline_ms or self._policy.deadline_ms) / 1000.0,
                fn=fn,
                args=args,
                kwargs=kwargs,
                cancel=cancel,
                future=future,
            )
            heapq.heappush(self._heap, item)
            self._metrics.admitted += 1
            self._metrics.pending = len(self._heap)
            self._cond.notify()
        self._ensure_workers()
        return future

    def _admit_locked(self, future: Future, wait_ms: int) -> bool:
        """Backpressure: wait for capacity up to wait_ms, else overflow."""
        wait_until = time.monotonic() + max(0, wait_ms) / 1000.0
        while len(self._heap) >= self._policy.queue_capacity:
            remaining = wait_until - time.monotonic()
            if remaining > 0:
                self._cond.wait(remaining)
                continue
            if self._policy.overflow is OverflowPolicy.DROP_OLDEST:
                evicted = self._evict_lowest_locked()
                if evicted is not None:
                    return True
            self._metrics.rejected += 1
            future.set_exception(
                AdmissionRejected(f"queue-full:{self._policy.pool}")
            )
            return False
        return True

    def _evict_lowest_locked(self) -> _Item | None:
        """Drop the lowest-priority pending item (drop_oldest policy)."""
        if not self._heap:
            return None
        victim = max(self._heap)  # highest (priority, seq) = least important
        self._heap.remove(victim)
        heapq.heapify(self._heap)
        victim.future.set_exception(
            WorkExpired(f"dropped:{self._policy.pool}")
        )
        self._metrics.dropped += 1
        return victim

    # -- worker pool -------------------------------------------------------

    def _ensure_workers(self) -> None:
        with self._cond:
            live = {t for t in self._threads if t.is_alive()}
            self._threads = live
            while len(live) < self._target_workers():
                worker = threading.Thread(
                    target=self._worker_loop,
                    name=f"bounded-{self._policy.pool}-{len(live)}",
                    daemon=True,
                )
                worker.start()
                live.add(worker)
            self._threads = live
            self._metrics.workers = len(live)

    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                while not self._closed and not self._heap:
                    self._cond.wait(1.0)
                if self._closed:
                    return
                # Adaptive shrink: over-quota workers exit when idle.
                self._refresh_budget()
                if len(self._threads) > self._target_workers():
                    self._threads.discard(threading.current_thread())
                    return
                item = self._pop_ready_locked()
                if item is None:
                    self._cond.wait(0.1)
                    continue
                self._running += 1
                self._metrics.pending = len(self._heap)
                self._metrics.running = self._running
            try:
                self._run_item(item)
            finally:
                with self._cond:
                    self._running -= 1
                    self._metrics.running = self._running
                    self._cond.notify()  # release a backpressure waiter

    def _pop_ready_locked(self) -> _Item | None:
        """Pop the first non-expired, non-cancelled item; shed expired."""
        while self._heap:
            item = heapq.heappop(self._heap)
            if item.future.set_running_or_notify_cancel() is False:
                self._metrics.cancelled += 1
                continue
            if item.cancel is not None and item.cancel.is_set():
                item.future.cancel()
                self._metrics.cancelled += 1
                continue
            if time.monotonic() > item.deadline:
                item.future.set_exception(
                    WorkExpired(f"deadline:{self._policy.pool}")
                )
                self._metrics.expired += 1
                continue
            return item
        return None

    @staticmethod
    def _run_item(item: _Item) -> None:
        if item.cancel is not None and item.cancel.is_set():
            item.future.cancel()
            return
        try:
            item.future.set_result(item.fn(*item.args, **item.kwargs))
        except BaseException as exc:  # propagate work failures to caller
            item.future.set_exception(exc)

    # -- lifecycle / metrics ------------------------------------------------

    def metrics(self) -> dict:
        with self._cond:
            snapshot = dict(vars(self._metrics))
        snapshot.update(
            {
                "pool": self._policy.pool,
                "work_class": self._policy.work_class,
                "quota": self._quota,
                "paused": self._paused,
                "capacity": self._policy.queue_capacity,
            }
        )
        return snapshot

    def shutdown(self, *, drain: bool = False) -> None:
        with self._cond:
            self._closed = True
            if not drain:
                while self._heap:
                    item = heapq.heappop(self._heap)
                    item.future.set_exception(
                        AdmissionRejected("executor-closed")
                    )
            self._cond.notify_all()
        for thread in list(self._threads):
            thread.join(timeout=5.0)


_executors: dict[str, BoundedExecutor] = {}
_executors_lock = threading.Lock()


def pool_for(policy: PoolPolicy) -> BoundedExecutor:
    """Process-wide executor registry: one pool object per pool name."""
    with _executors_lock:
        executor = _executors.get(policy.pool)
        if executor is None:
            executor = BoundedExecutor(policy)
            _executors[policy.pool] = executor
        return executor


__all__ = [
    "AdmissionRejected",
    "BoundedExecutor",
    "OverflowPolicy",
    "PoolMetrics",
    "PoolPaused",
    "PoolPolicy",
    "WorkExpired",
    "pool_for",
]
