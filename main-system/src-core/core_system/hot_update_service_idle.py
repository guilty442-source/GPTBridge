"""Hot-update idle replacement mixin — A181/A182/A183 deferred replacement.

Provides the idle-loop, pending replacement, and lifecycle methods
for the HotUpdateService class.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import types
from typing import Any

from core_system.hot_update_service_helpers import _cleanup_module

_logger = logging.getLogger("gptbridge.hot_update")


class HotUpdateIdleMixin:
    """Idle-period deferred replacement methods for HotUpdateService."""

    def is_idle(self) -> bool:
        """Return True when the system has no in-flight command tasks.

        This is the gate for applying deferred resource-holding module
        replacements — they must only run when no request is being served.
        """
        command_tasks = getattr(self.app, "_command_tasks", None)
        if command_tasks is None:
            return True
        return len(command_tasks) == 0

    def pending_replacement_count(self) -> int:
        """Return the number of resource-holding modules awaiting replacement."""
        with self._pending_lock:
            return len(self._pending_replacements)

    def apply_pending_replacements(self) -> types.SimpleNamespace:
        """Apply deferred resource-holding module replacements during idle.

        Should be called from a periodic idle-check loop.  Only runs when
        ``is_idle()`` is True.  Each module is cleaned up before reload so
        resources (sockets, DB connections) are released gracefully.

        Returns a SimpleNamespace with ``applied``, ``errors``, and
        ``skipped`` attributes.
        """
        if not self.is_idle():
            return types.SimpleNamespace(
                applied=[], errors=[], skipped=[], idle=False,
            )

        # Concurrent reload protection.
        if not self._reload_lock.acquire(blocking=False):
            return types.SimpleNamespace(
                applied=[], errors=[], skipped=[], idle=True,
                error="reload-in-progress",
            )

        try:
            return self._apply_pending_locked()
        finally:
            self._reload_lock.release()

    def _apply_pending_locked(self) -> types.SimpleNamespace:
        import sys

        applied: list[str] = []
        errors: list[str] = []
        skipped: list[str] = []

        with self._pending_lock:
            pending = list(self._pending_replacements)
            self._pending_replacements.clear()

        for module_name, old_module in pending:
            # Re-check: the module may have been reloaded by another path.
            current = sys.modules.get(module_name)
            if current is None or current is not old_module:
                skipped.append(module_name)
                continue
            try:
                # Graceful cleanup before reload.
                _cleanup_module(current)
                importlib.reload(current)
                applied.append(module_name)
                _logger.info(
                    "hot_reload_applied_deferred module=%s", module_name,
                )
            except Exception as error:
                errors.append(f"{module_name}: {error}")
                # Roll back to the snapshot.
                snapshot = self._pending_snapshots.pop(module_name, None)
                if snapshot and isinstance(current, types.ModuleType):
                    current.__dict__.clear()
                    current.__dict__.update(snapshot)

        # Clean up snapshots for successfully applied modules.
        for name in applied:
            self._pending_snapshots.pop(name, None)

        if applied:
            self._persist_reload_protection(applied)
            self._update_source_hashes(applied)
            # Post-reload health verification.
            if not self._post_reload_health_check():
                _logger.warning(
                    "hot_reload_health_check_failed_after_deferred — "
                    "applied modules may need attention",
                )

        return types.SimpleNamespace(
            applied=applied,
            errors=errors,
            skipped=skipped,
            idle=True,
        )

    async def _idle_loop(self) -> None:
        """Periodically apply deferred resource-holding module replacements.

        Runs only when the system is idle and there are pending resource-holding
        module replacements.  This prevents half-replaced modules from being used
        by in-flight requests.
        """
        while True:
            if self._stop_event is not None and self._stop_event.is_set():
                break
            try:
                if self.pending_replacement_count() > 0 and self.is_idle():
                    await asyncio.to_thread(self.apply_pending_replacements)
            except Exception:
                # Best-effort: never let the idle loop die.
                pass
            try:
                await asyncio.sleep(self.interval_seconds)
            except asyncio.CancelledError:
                break

    async def start(self) -> None:
        """Begin the idle-period deferred-replacement loop."""
        if self._idle_task is not None and not self._idle_task.done():
            return
        self._stop_event = asyncio.Event()
        self._idle_task = asyncio.create_task(
            self._idle_loop(),
            name="hot-update-idle-loop",
        )

    async def stop(self) -> None:
        """Cancel the idle-period deferred-replacement loop."""
        if self._idle_task is None:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        self._idle_task.cancel()
        try:
            await self._idle_task
        except asyncio.CancelledError:
            pass
        self._idle_task = None
        self._stop_event = None

    async def _apply(
        self,
        _plan: dict[str, Any],
        *,
        repairs_completed: bool = False,
    ) -> None:
        del repairs_completed
        raise PermissionError("PERMISSION_DENIED")
