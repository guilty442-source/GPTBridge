"""Tool process post-start watching."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any


class ToolWatcherMixin:
    """Post-start process watching utilities for toolbox tools."""

    async def _watch_started_tool(
        self,
        request_id: str,
        tool_id: str,
        runtime_path: Path,
        source_runtime: bool,
        process: asyncio.subprocess.Process,
        close_program_on_exit: bool = False,
    ) -> None:
        try:
            await process.wait()
        finally:
            cancelled = await self._release_tool_process(request_id)
            if (
                close_program_on_exit
                and not cancelled
                and tool_id not in self._force_closed_tool_ids
            ):
                try:
                    await self.force_close_tool(
                        {
                            "tool_id": tool_id,
                            "request_id": f"window-close-{uuid.uuid4().hex}",
                            "reason": "independent-tool-window-closed",
                        }
                    )
                finally:
                    await self.update_status(tool_id, "stopped")
                return
            still_running = bool(
                self._running_source_runtime_process_ids(runtime_path)
                if source_runtime
                else self._running_executable_process_ids(runtime_path)
            )
            await self.update_status(
                tool_id,
                "running" if still_running and not cancelled else "stopped",
            )
            if not cancelled and not still_running:
                self._schedule_background_restart(tool_id)