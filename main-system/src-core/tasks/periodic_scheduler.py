"""Shared periodic scheduler — one timer loop for infrequent jobs (§10.63 R3).

Each registered job keeps its own due-gate logic (adaptive interval, idle
gate, is_due checks); the scheduler only supplies a single deadline-driven
asyncio loop instead of one task per service. Per-job runs are awaited
serially with a bounded timeout; a failing job is recorded and isolated —
it never kills the loop or other jobs.

``jobs()`` returns the periodic-work inventory (name / interval / last run
/ last duration / last error) — the evidence for the §10.63 acceptance
"fixed-period work <= 6; each cycle bounded".
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable

_logger = logging.getLogger("gptbridge.periodic_scheduler")

Tick = Callable[[], Awaitable[None]]

BASE_TICK_SECONDS = 15.0
DEFAULT_JOB_TIMEOUT_SECONDS = 120.0


class PeriodicScheduler:
    def __init__(
        self,
        *,
        tick_seconds: float = BASE_TICK_SECONDS,
        job_timeout_s: float = DEFAULT_JOB_TIMEOUT_SECONDS,
    ) -> None:
        self._tick_seconds = tick_seconds
        self._job_timeout_s = job_timeout_s
        self._jobs: dict[str, dict[str, Any]] = {}
        self._stop = asyncio.Event()
        self._task: asyncio.Task[Any] | None = None

    def register(
        self,
        name: str,
        interval_s: float,
        tick: Tick,
        *,
        run_immediately: bool = False,
        timeout_s: float | None = None,
    ) -> None:
        """Register a periodic job. ``tick`` is an awaitable called when
        ``interval_s`` has elapsed since its last start. Services keep
        their own internal gating inside ``tick``."""
        self._jobs[name] = {
            "interval_s": float(interval_s),
            "tick": tick,
            "timeout_s": timeout_s or self._job_timeout_s,
            "next_due": (
                time.monotonic()
                if run_immediately
                else time.monotonic() + float(interval_s)
            ),
            "last_started": None,
            "last_duration_ms": None,
            "last_error": None,
            "run_count": 0,
        }
        # Lazily start the shared loop when the first job registers and an
        # event loop is available (services start at different phases).
        if self._task is None or self._task.done():
            self.start()

    def unregister(self, name: str) -> None:
        self._jobs.pop(name, None)

    def start(self) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            return {"status": "already_running"}
        self._stop.clear()
        try:
            self._task = asyncio.create_task(
                self._loop(), name="periodic-scheduler"
            )
        except RuntimeError:
            self._task = None
            return {"status": "no_event_loop"}
        return {"status": "started"}

    async def stop(self) -> None:
        self._stop.set()
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    def jobs(self) -> list[dict[str, Any]]:
        """Periodic-work inventory for the §10.63 acceptance surface."""
        out = []
        for name, job in self._jobs.items():
            out.append(
                {
                    "name": name,
                    "interval_s": job["interval_s"],
                    "last_started": job["last_started"],
                    "last_duration_ms": job["last_duration_ms"],
                    "last_error": job["last_error"],
                    "run_count": job["run_count"],
                }
            )
        return out

    async def _loop(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            next_due = now + self._tick_seconds
            for name, job in list(self._jobs.items()):
                if self._stop.is_set():
                    break
                if job["next_due"] > now:
                    next_due = min(next_due, job["next_due"])
                    continue
                job["next_due"] = now + job["interval_s"]
                job["last_started"] = time.time()
                mark = time.monotonic()
                try:
                    await asyncio.wait_for(
                        job["tick"](), timeout=job["timeout_s"]
                    )
                    job["last_error"] = None
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    job["last_error"] = f"{type(error).__name__}: {error}"
                    _logger.warning("periodic job %s failed: %s", name, error)
                job["last_duration_ms"] = round(
                    (time.monotonic() - mark) * 1000, 1
                )
                job["run_count"] += 1
                next_due = min(next_due, job["next_due"])
            wait_s = max(0.05, next_due - time.monotonic())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait_s)
            except asyncio.TimeoutError:
                continue


__all__ = ["PeriodicScheduler", "BASE_TICK_SECONDS"]
