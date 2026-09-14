"""Hot-update idle loop — split from HotUpdateService for A430 compliance."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .hot_update_constants import _IDLE_WAIT_TIMEOUT


class IdleLoopManager:
    """Manage the idle-loop lifecycle for pending replacements."""

    def __init__(self, app: Any, interval_seconds: float) -> None:
        self.app = app
        self.interval_seconds = max(10.0, interval_seconds)
        self._stop_event: asyncio.Event | None = None
        self._idle_task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        """Start the idle loop."""
        self._stop_event = asyncio.Event()
        self._idle_task = asyncio.create_task(
            self._idle_loop(),
            name="hot-update-idle-loop",
        )

    async def stop(self) -> None:
        """Stop the idle loop."""
        if self._idle_task is not None:
            self._stop_event.set()
            try:
                await asyncio.wait_for(self._idle_task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._idle_task = None

    async def _idle_loop(self) -> None:
        """Background loop: apply pending resource-holding module replacements."""
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval_seconds)
            except asyncio.TimeoutError:
                pass
            if self._stop_event.is_set():
                break
            try:
                await self._apply_pending_replacements()
            except Exception as error:
                _logger.warning("hot_update_idle_loop_error: %s", error)

    async def _apply_pending_replacements(self) -> None:
        """Replace resource-holding modules during idle periods."""
        # This will be called from the main HotUpdateService
        # which has access to _pending_replacements and _pending_snapshots
        pass