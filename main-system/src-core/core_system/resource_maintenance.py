from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import time
from collections.abc import Callable
from typing import Any


DEFAULT_INTERVAL_SECONDS = 15 * 60.0
DEFAULT_MINIMUM_IDLE_SECONDS = 2 * 60.0


def release_unused_memory() -> dict[str, Any]:
    """Release unreachable objects and return idle pages to Windows."""

    collected = int(gc.collect())
    working_set_trimmed = False
    if os.name == "nt":
        try:
            import ctypes

            process = ctypes.windll.kernel32.GetCurrentProcess()
            working_set_trimmed = bool(ctypes.windll.psapi.EmptyWorkingSet(process))
        except (AttributeError, OSError):
            working_set_trimmed = False
    return {
        "collected_objects": collected,
        "working_set_trimmed": working_set_trimmed,
        "completed_at": time.time(),
    }


class IdleMemoryMaintainer:
    """Run infrequent main-system memory maintenance only while idle."""

    def __init__(
        self,
        *,
        is_busy: Callable[[], bool] | None = None,
        before_release: Callable[[], Any] | None = None,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        minimum_idle_seconds: float = DEFAULT_MINIMUM_IDLE_SECONDS,
    ) -> None:
        self.is_busy = is_busy or (lambda: False)
        self.before_release = before_release
        self.interval_seconds = max(60.0, float(interval_seconds))
        self.minimum_idle_seconds = max(30.0, float(minimum_idle_seconds))
        self.last_activity = time.monotonic()
        self.last_result: dict[str, Any] | None = None

    def mark_activity(self) -> None:
        self.last_activity = time.monotonic()

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "owner": "main-system",
            "interval_seconds": self.interval_seconds,
            "minimum_idle_seconds": self.minimum_idle_seconds,
            "last_run": dict(self.last_result) if self.last_result else None,
        }

    def release(self) -> dict[str, Any]:
        if self.before_release is not None:
            self.before_release()
        return release_unused_memory()

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=self.interval_seconds)
                break
            except asyncio.TimeoutError:
                pass
            if self.is_busy():
                self.mark_activity()
                continue
            if time.monotonic() - self.last_activity < self.minimum_idle_seconds:
                continue
            self.last_result = await asyncio.to_thread(self.release)

    @staticmethod
    async def stop(task: asyncio.Task[Any] | None) -> None:
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
