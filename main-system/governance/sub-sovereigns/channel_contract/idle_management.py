"""Channel Contract Sync Sub-Sovereign — Idle Module Management."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from core_system.sovereign_utils import _iso_now


class IdleManagementMixin:
    """Idle module monitor and on-demand restart."""

    _tool_last_activity: dict[str, float]
    _idle_stopped_tools: set[str]
    _idle_monitor_task: asyncio.Task[Any] | None
    _resident_tool_ids: set[str]
    _toolbox: Any
    _started: bool
    app: Any

    def mark_tool_activity(self, tool_id: str) -> None:
        """Mark that a module received activity (execution request or start)."""
        self._tool_last_activity[tool_id] = time.monotonic()
        self._idle_stopped_tools.discard(tool_id)

    def is_tool_running(self, tool_id: str) -> bool:
        """Check whether a module is currently running (has a started process)."""
        toolbox = self._toolbox
        if toolbox is None:
            return False
        started = getattr(toolbox, "_started_request_by_tool", {})
        return tool_id in started

    async def ensure_tool_running(self, tool_id: str) -> bool:
        """Ensure a module is running; auto-start it if it was idle-stopped."""
        if self.is_tool_running(tool_id):
            self.mark_tool_activity(tool_id)
            return True

        toolbox = self._toolbox
        if toolbox is None:
            return False

        permission = getattr(self.app, "permission_sovereign", None)
        if permission is None or not permission.can_start_tool(tool_id):
            return False

        try:
            result = await toolbox.start_tool(
                {
                    "tool_id": tool_id,
                    "request_id": f"idle-restart-{tool_id}-{time.time_ns()}",
                    "background": True,
                }
            )
        except Exception:
            return False

        if result.get("ok") is True:
            self.mark_tool_activity(tool_id)
            return True
        return False

    async def start_idle_monitor(self) -> None:
        """Start the idle-module monitor loop."""
        if self._idle_monitor_task is None:
            self._idle_monitor_task = asyncio.create_task(
                self._idle_monitor_loop(),
                name="channel-contract-sync-idle-monitor",
            )

    async def stop_idle_monitor(self) -> None:
        if self._idle_monitor_task is not None:
            self._idle_monitor_task.cancel()
            with _suppress(asyncio.CancelledError):
                await self._idle_monitor_task
            self._idle_monitor_task = None

    async def _idle_monitor_loop(self) -> None:
        while self._started:
            await asyncio.sleep(IDLE_MONITOR_INTERVAL_SECONDS)
            if not self._started:
                break
            await self._check_idle_tools()

    async def _check_idle_tools(self) -> None:
        toolbox = self._toolbox
        if toolbox is None:
            return

        started_by_tool: dict[str, str] = getattr(
            toolbox, "_started_request_by_tool", {}
        )
        active_by_tool: dict[str, str] = getattr(
            toolbox, "_active_request_by_tool", {}
        )
        now = time.monotonic()

        idle_tool_ids: list[str] = []
        for tool_id in list(started_by_tool.keys()):
            if tool_id in self._resident_tool_ids:
                continue

            if tool_id in active_by_tool:
                self._tool_last_activity[tool_id] = now
                continue

            last_activity = self._tool_last_activity.get(tool_id)
            if last_activity is None:
                self._tool_last_activity[tool_id] = now
                continue

            idle_seconds = now - last_activity
            if idle_seconds < IDLE_TIMEOUT_SECONDS:
                continue

            permission = getattr(self.app, "permission_sovereign", None)
            if permission is None or not permission.can_start_tool(tool_id):
                continue
            idle_tool_ids.append(tool_id)

        async def _stop_one(tid: str) -> str | None:
            try:
                await toolbox.stop_tool({"tool_id": tid})
            except Exception:
                return None
            return tid

        stopped = await asyncio.gather(
            *(_stop_one(tid) for tid in idle_tool_ids),
            return_exceptions=False,
        )
        for tid in stopped:
            if tid is None:
                continue
            self._idle_stopped_tools.add(tid)
            self._tool_last_activity.pop(tid, None)