"""Idle-loop and deferred-replacement mixin for HotUpdateService (A185 split).

Manages the periodic idle loop that applies deferred resource-holding
module replacements when the system has no in-flight command tasks.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import types
from typing import Any

from .hot_update_service_helpers import cleanup_module

_logger = logging.getLogger("gptbridge.hot_update")


class HotUpdateIdleMixin:
    """Deferred resource-holding module replacement and idle loop."""

    app: Any
    interval_seconds: float
    _stop_event: asyncio.Event | None
    _idle_task: asyncio.Task[Any] | None
    _reload_lock: Any
    _pending_lock: Any
    _pending_replacements: list
    _pending_snapshots: dict

    def apply_pending_replacements(self) -> types.SimpleNamespace:
        """Apply deferred resource-holding module replacements during idle."""
        if not self.is_idle():
            return types.SimpleNamespace(
                applied=[], errors=[], skipped=[], idle=False)
        if not self._reload_lock.acquire(blocking=False):
            return types.SimpleNamespace(
                applied=[], errors=[], skipped=[], idle=True,
                error="reload-in-progress")
        try:
            return self._apply_pending_locked()
        finally:
            self._reload_lock.release()

    def _apply_pending_locked(self) -> types.SimpleNamespace:
        applied: list[str] = []
        errors: list[str] = []
        skipped: list[str] = []
        with self._pending_lock:
            pending = list(self._pending_replacements)
            self._pending_replacements.clear()
        for module_name, old_module in pending:
            current = type(self).__dict__.get("_get_current_module", lambda s, n: __import__("sys").modules.get(n))(self, module_name)
            if current is None or current is not old_module:
                skipped.append(module_name)
                continue
            try:
                cleanup_module(current)
                importlib.reload(current)
                applied.append(module_name)
                _logger.info("hot_reload_applied_deferred module=%s", module_name)
            except Exception as error:
                errors.append(f"{module_name}: {error}")
                snapshot = self._pending_snapshots.pop(module_name, None)
                if snapshot and isinstance(current, types.ModuleType):
                    current.__dict__.clear()
                    current.__dict__.update(snapshot)
        for name in applied:
            self._pending_snapshots.pop(name, None)
        if applied:
            self._persist_reload_protection(applied)
            self._update_source_hashes(applied)
            if not self._post_reload_health_check():
                _logger.warning(
                    "hot_reload_health_check_failed_after_deferred "
                    "applied modules may need attention")
        return types.SimpleNamespace(
            applied=applied, errors=errors, skipped=skipped, idle=True)

    async def _idle_loop(self) -> None:
        """Periodically apply deferred resource-holding module replacements."""
        import sys
        while True:
            if self._stop_event is not None and self._stop_event.is_set():
                break
            try:
                if self.pending_replacement_count() > 0 and self.is_idle():
                    await asyncio.to_thread(self.apply_pending_replacements)
            except Exception:
                pass
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break

    async def _idle_tick(self) -> None:
        """單次閒置檢查——供 automation core 外部驅動。"""
        if self.pending_replacement_count() > 0 and self.is_idle():
            try:
                await asyncio.to_thread(self.apply_pending_replacements)
            except Exception:
                pass

    async def start(self) -> None:
        """Begin the idle-period deferred-replacement loop.

        §1.1 自動化集中：automation core 為唯一註冊點；kill-switch
        拒絕時不回落私有迴圈。
        """
        if self._idle_task is not None and not self._idle_task.done():
            return
        core = getattr(getattr(self, "app", None), "automation_core", None)
        if core is not None:
            if core.register_flow(
                "hot-update-idle",
                self._idle_tick,
                interval_s=self.interval_seconds,
                pausable=True,
            ):
                self._core_driven = True
                return
            self._core_driven = False
            return
        self._stop_event = asyncio.Event()
        self._idle_task = asyncio.create_task(
            self._idle_loop(), name="hot-update-idle-loop")

    async def stop(self) -> None:
        """Cancel the idle loop and cleanup resources."""
        if getattr(self, "_core_driven", False):
            core = getattr(getattr(self, "app", None), "automation_core", None)
            if core is not None:
                core.unregister("hot-update-idle")
            self._core_driven = False
        if self._idle_task is not None:
            if self._stop_event is not None:
                self._stop_event.set()
            self._idle_task.cancel()
            try:
                await self._idle_task
            except asyncio.CancelledError:
                pass
            self._idle_task = None
            self._stop_event = None
        if self.pending_replacement_count() > 0:
            try:
                self.apply_pending_replacements()
            except Exception as e:
                _logger.warning(
                    "Error applying pending replacements during shutdown: %s", e)
        self._pending_snapshots.clear()
        self._pending_replacements.clear()
        if hasattr(self, '_circuit_breaker'):
            self._circuit_breaker.reset()


__all__ = ["HotUpdateIdleMixin"]
