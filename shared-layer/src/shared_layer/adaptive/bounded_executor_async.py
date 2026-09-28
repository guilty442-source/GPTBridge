"""Async bounded executor — asyncio twin of :mod:`bounded_executor`.

Same six-property queue contract (capacity / priority / deadline /
backpressure / cancellation / drop-reject) for coroutine workloads.
Unlike the thread variant, cancellation is *preemptive*: running
coroutines are cancelled via ``Task.cancel`` and a cancel event raced
against the work, and the run-phase deadline is enforced with
``asyncio.wait_for`` on the remaining budget.

Sizing follows the same governor contract: the worker-task count is the
``concurrency-budget/v1`` class quota clamped into the module-declared
envelope, refreshed on every dequeue; missing/stale state falls back to
``max_workers`` (fail-open).
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
import logging
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Coroutine

from .bounded_executor import (
    AdmissionRejected,
    OverflowPolicy,
    PoolMetrics,
    PoolPaused,
    PoolPolicy,
    WorkExpired,
)
from .budget_source import class_quota
from .types import PriorityClass

_logger = logging.getLogger("gptbridge.bounded_executor_async")

_STATE_FILE = (
    Path(__file__).resolve().parents[4]
    / "main-system" / "runtime" / "state" / "resource-governor.json"
)


class _AsyncItem:
    __slots__ = ("priority", "seq", "deadline", "factory", "cancel", "future")

    def __init__(
        self,
        priority: int,
        seq: int,
        deadline: float,
        factory: Callable[[], Coroutine[Any, Any, Any]],
        cancel: asyncio.Event | None,
        future: asyncio.Future,
    ) -> None:
        self.priority = priority
        self.seq = seq
        self.deadline = deadline
        self.factory = factory
        self.cancel = cancel
        self.future = future

    def __lt__(self, other: "_AsyncItem") -> bool:
        return (self.priority, self.seq) < (other.priority, other.seq)


class AsyncBoundedExecutor:
    """Bounded priority queue drained by governor-sized worker tasks."""

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
        self._cond = asyncio.Condition()
        self._heap: list[_AsyncItem] = []
        self._seq = itertools.count()
        self._workers: set[asyncio.Task] = set()
        self._closed = False
        self._metrics = PoolMetrics()
        self._quota: int | None = None
        self._paused = False
        self._budget_checked = 0.0

    def _refresh_budget(self) -> None:
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
        return self._quota if self._quota is not None else self._policy.max_workers

    async def submit(
        self,
        factory: Callable[[], Coroutine[Any, Any, Any]],
        *,
        priority: PriorityClass = PriorityClass.INTERACTIVE,
        deadline_ms: int | None = None,
        cancel: asyncio.Event | None = None,
        wait_ms: int = 0,
    ) -> Any:
        """Admit a coroutine factory; awaits and returns its result.

        Raises :class:`AdmissionRejected` (full+reject), :class:`PoolPaused`
        (governor shed), :class:`WorkExpired` (queue deadline).
        """
        self._refresh_budget()
        if self._closed:
            raise AdmissionRejected("executor-closed")
        if self._paused:
            self._metrics.rejected += 1
            raise PoolPaused(self._policy.work_class)
        if cancel is not None and cancel.is_set():
            self._metrics.cancelled += 1
            raise asyncio.CancelledError()
        await self._await_capacity(wait_ms)
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        item = _AsyncItem(
            priority=priority.rank,
            seq=next(self._seq),
            deadline=time.monotonic()
            + (deadline_ms or self._policy.deadline_ms) / 1000.0,
            factory=factory,
            cancel=cancel,
            future=future,
        )
        async with self._cond:
            heapq.heappush(self._heap, item)
            self._metrics.admitted += 1
            self._metrics.pending = len(self._heap)
            self._cond.notify()
            self._ensure_workers()
        return await future

    async def _await_capacity(self, wait_ms: int) -> None:
        """Backpressure: wait for room up to wait_ms; then overflow policy."""
        wait_until = time.monotonic() + max(0, wait_ms) / 1000.0
        async with self._cond:
            while len(self._heap) >= self._policy.queue_capacity:
                remaining = wait_until - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._cond.wait(), remaining)
                except asyncio.TimeoutError:
                    break
            if len(self._heap) < self._policy.queue_capacity:
                return
            if self._policy.overflow is OverflowPolicy.DROP_OLDEST:
                if self._evict_lowest() is not None:
                    return
            self._metrics.rejected += 1
            raise AdmissionRejected(f"queue-full:{self._policy.pool}")

    def _evict_lowest(self) -> _AsyncItem | None:
        if not self._heap:
            return None
        victim = max(self._heap)
        self._heap.remove(victim)
        heapq.heapify(self._heap)
        if not victim.future.done():
            victim.future.set_exception(
                WorkExpired(f"dropped:{self._policy.pool}")
            )
        self._metrics.dropped += 1
        return victim

    def _ensure_workers(self) -> None:
        live = {t for t in self._workers if not t.done()}
        self._workers = live
        while len(live) < self._target_workers():
            task = asyncio.create_task(
                self._worker_loop(),
                name=f"abounded-{self._policy.pool}-{len(live)}",
            )
            live.add(task)
        self._workers = live
        self._metrics.workers = len(live)

    async def _worker_loop(self) -> None:
        while True:
            item = await self._pop_ready()
            if item is None:
                if self._closed:
                    return
                # Over-quota workers retire when idle.
                self._refresh_budget()
                if len(self._workers) > self._target_workers():
                    self._workers.discard(asyncio.current_task())
                    return
                continue
            await self._run_item(item)

    async def _pop_ready(self) -> _AsyncItem | None:
        async with self._cond:
            while self._heap:
                item = heapq.heappop(self._heap)
                self._metrics.pending = len(self._heap)
                if item.future.cancelled():
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
                self._metrics.running += 1
                return item
            try:
                await asyncio.wait_for(self._cond.wait(), 0.5)
            except asyncio.TimeoutError:
                return None
        return None

    async def _run_item(self, item: _AsyncItem) -> None:
        """Run with remaining-deadline timeout and preemptive cancel."""
        remaining = item.deadline - time.monotonic()
        work = asyncio.ensure_future(item.factory())
        cancel_wait = (
            asyncio.ensure_future(item.cancel.wait())
            if item.cancel is not None
            else None
        )
        done = {work}
        if cancel_wait is not None:
            done.add(cancel_wait)
        try:
            finished, _ = await asyncio.wait(
                done, timeout=max(0.001, remaining),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if work in finished:
                item.future.set_result(work.result())
            else:
                work.cancel()
                if not item.future.done():
                    item.future.cancel()
                self._metrics.cancelled += 1
        except asyncio.TimeoutError:
            work.cancel()
            if not item.future.done():
                item.future.set_exception(
                    WorkExpired(f"deadline:{self._policy.pool}")
                )
            self._metrics.expired += 1
        except asyncio.CancelledError:
            work.cancel()
            if not item.future.done():
                item.future.cancel()
            self._metrics.cancelled += 1
        except BaseException as exc:
            if not item.future.done():
                item.future.set_exception(exc)
        finally:
            self._metrics.running -= 1
            if cancel_wait is not None:
                cancel_wait.cancel()
            async with self._cond:
                self._cond.notify()  # release backpressure waiters

    def metrics(self) -> dict:
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

    async def shutdown(self, *, drain: bool = False) -> None:
        self._closed = True
        if not drain:
            async with self._cond:
                while self._heap:
                    item = heapq.heappop(self._heap)
                    if not item.future.done():
                        item.future.set_exception(
                            AdmissionRejected("executor-closed")
                        )
        async with self._cond:
            self._cond.notify_all()
        for task in list(self._workers):
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)


__all__ = [
    "AdmissionRejected",
    "AsyncBoundedExecutor",
    "PoolPaused",
    "WorkExpired",
]
