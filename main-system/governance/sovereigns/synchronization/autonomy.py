"""Synchronization Sovereign — Autonomous Supervision.

Continuous supervision of sync sub-sovereigns: failure detection, restart
adjudication, and state persistence.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .._base import SovereignBase


class SyncAutonomyMixin:
    """Autonomous supervision for sync sub-sovereigns."""

    _autonomy_task: asyncio.Task[Any] | None
    _autonomy_stop: asyncio.Event
    _child_supervision: dict[str, dict[str, Any]]
    app: Any
    _started: bool

    def _start_autonomy_loop(self) -> None:
        if self._autonomy_task is None or self._autonomy_task.done():
            self._autonomy_stop.clear()
            try:
                self._autonomy_task = asyncio.create_task(
                    self._autonomy_loop(),
                    name="synchronization-sovereign-autonomy",
                )
            except RuntimeError:
                self._autonomy_task = None

    async def _stop_autonomy_loop(self) -> None:
        task = self._autonomy_task
        self._autonomy_task = None
        self._autonomy_stop.set()
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def _autonomy_loop(self) -> None:
        while not self._autonomy_stop.is_set():
            try:
                await self._supervise_children()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            try:
                await asyncio.wait_for(
                    self._autonomy_stop.wait(),
                    timeout=15.0,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                raise

    async def _supervise_children(self) -> None:
        """Detect stopped children and adjudicate bounded restarts (A322)."""
        from governance.registries import parent_of, resolve_sovereign

        now = time.monotonic()
        for child_id, child in self._all_children().items():
            if bool(getattr(child, "_started", False)):
                watch = self._child_supervision.get(child_id)
                if watch is not None and watch.get("state") != "started":
                    watch["state"] = "started"
                    watch["recovered_at"] = self._iso_now()
                    watch.pop("quarantined", None)
                continue

            watch = self._child_supervision.setdefault(
                child_id, {"state": "started", "restart_attempts": 0}
            )
            if watch.get("state") == "started":
                parent_id = parent_of(child_id)
                parent = (
                    self
                    if parent_id == self.sovereign_id
                    else resolve_sovereign(self.app, parent_id)
                )
                if parent is not None:
                    try:
                        parent.record_child_failure(child_id)
                    except Exception:
                        pass
                watch["state"] = "stopped"
                watch["stopped_at"] = self._iso_now()

            if watch.get("quarantined"):
                continue
            parent_id = parent_of(child_id)
            parent = (
                self
                if parent_id == self.sovereign_id
                else resolve_sovereign(self.app, parent_id)
            )
            if parent is None:
                continue
            if parent.child_failure_count(child_id) > 3:
                watch["quarantined"] = True
                watch["quarantined_at"] = self._iso_now()
                continue
            last_attempt = float(watch.get("last_attempt") or 0.0)
            if now - last_attempt < 60.0:
                continue
            executor = getattr(self.app, "sovereign_stack_executor", None)
            if executor is None:
                continue
            watch["last_attempt"] = now
            watch["restart_attempts"] = int(watch.get("restart_attempts") or 0) + 1
            try:
                watch["last_result"] = await executor.restart_child(self, child_id)
            except Exception as error:
                watch["last_result"] = {
                    "ok": False,
                    "error": f"{type(error).__name__}: {error}",
                }
