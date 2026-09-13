"""Daily global cleaner — module self-cleanup sweep mixin.

Provides the module self-cleanup sweep methods for the
DailyGlobalCleanerService class.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any


class DailyGlobalCleanerSweepMixin:
    """Module self-cleanup sweep methods for DailyGlobalCleanerService."""

    async def _run_module_self_cleanup_sweep(self) -> dict[str, Any]:
        """Collect per-module self-cleanup results for the daily cycle.

        Execution stays devolved: running governed tools receive the reserved
        ``toolbox_run_local_cleanup`` command on their own runtime; stopped
        tools contribute their persisted last-boot cleanup record; in-process
        modules (``main-system``) run the shared bounded cleanup directly.
        """

        from governance_rule.execution.tool_runtime.tool_local_cleanup import (
            read_local_cleanup_state,
            run_local_cleanup,
            write_local_cleanup_state,
        )

        started_at = self._iso_now()
        modules: list[dict[str, Any]] = []
        runtime_ready = self._runtime_ready()

        for module_id in self.IN_PROCESS_MODULE_IDS:
            module_root = Path(self.app.project_root) / module_id
            if not runtime_ready:
                modules.append(
                    {
                        "module_id": module_id,
                        "mode": "in-process",
                        "running": True,
                        "ok": False,
                        "deferred": True,
                        "reason": "RUNTIME_NOT_READY",
                    }
                )
                continue
            try:
                cleanup = await asyncio.to_thread(
                    run_local_cleanup, module_id, module_root
                )
                await asyncio.to_thread(
                    write_local_cleanup_state, module_root, cleanup
                )
            except Exception as error:
                cleanup = {
                    "ok": False,
                    "error_code": "MODULE_CLEANUP_EXCEPTION",
                    "message": f"{type(error).__name__}: {error}",
                }
            modules.append(
                {
                    "module_id": module_id,
                    "mode": "in-process",
                    "running": True,
                    "ok": bool(cleanup.get("ok")),
                    "result": cleanup,
                }
            )

        toolbox = self.app.toolbox_service
        permission = self._permission_master_entry()
        tools: list[dict[str, Any]] = []
        if toolbox is not None and permission is not None:
            try:
                listing = await toolbox.list_tools()
                if isinstance(listing, dict) and isinstance(
                    listing.get("tools"), list
                ):
                    tools = listing["tools"]
            except Exception:
                tools = []
        for tool in tools:
            tool_id = str(tool.get("id") or "").strip()
            if not tool_id or tool_id in self.EXCLUDED_MODULE_IDS:
                continue
            if str(tool.get("status") or "") == "running":
                modules.append(
                    await self._request_module_cleanup(
                        tool_id, toolbox, permission
                    )
                )
                continue
            try:
                tool_dir = toolbox._tool_directory_for_id(tool_id)
            except Exception:
                modules.append(
                    {
                        "module_id": tool_id,
                        "mode": "last-recorded",
                        "running": False,
                        "ok": False,
                        "error_code": "MODULE_UNAVAILABLE",
                    }
                )
                continue
            last = await asyncio.to_thread(read_local_cleanup_state, tool_dir)
            recorded_epoch = (
                self._parse_iso_epoch(last.get("completed_at"))
                if isinstance(last, dict)
                else 0.0
            )
            stale = (
                last is None
                or recorded_epoch <= 0
                or (time.time() - recorded_epoch) > self.STALE_RECORD_SECONDS
            )
            modules.append(
                {
                    "module_id": tool_id,
                    "mode": "last-recorded",
                    "running": False,
                    "ok": bool(last and last.get("ok")) and not stale,
                    "recorded": last is not None,
                    "stale": stale,
                    "reason": (
                        "STALE_CLEANUP_RECORD" if stale and last else ""
                    ),
                    "result": last,
                }
            )

        cleaned_bytes_total = 0
        for module in modules:
            result = module.get("result")
            if isinstance(result, dict):
                try:
                    cleaned_bytes_total += int(result.get("cleaned_bytes") or 0)
                except (TypeError, ValueError):
                    continue
        report = {
            "operation": "module-self-cleanup-sweep",
            "authority": "health-maintenance-test-sub-sovereign",
            "execution": "devolved-per-module",
            "command": self.MODULE_CLEANUP_COMMAND,
            "started_at": started_at,
            "completed_at": self._iso_now(),
            "module_count": len(modules),
            "commanded_count": sum(
                1 for module in modules if module.get("mode") == "commanded"
            ),
            "deferred_count": sum(
                1 for module in modules if module.get("deferred")
            ),
            "stale_count": sum(
                1 for module in modules if module.get("stale")
            ),
            "failed_count": sum(
                1 for module in modules if not module.get("ok")
            ),
            "runtime_ready": runtime_ready,
            "ok": all(module.get("ok") for module in modules),
            "cleaned_bytes_total": cleaned_bytes_total,
            "modules": modules,
        }
        return report

    async def _request_module_cleanup(
        self,
        tool_id: str,
        toolbox: Any,
        permission: Any,
    ) -> dict[str, Any]:
        request_id = f"module-cleanup-{tool_id}-{time.time_ns()}"
        queued = await toolbox.request_tool_execution(
            {
                "tool_id": tool_id,
                "request_id": request_id,
                "_governed_command": self.MODULE_CLEANUP_COMMAND,
            }
        )
        if queued.get("ok") is not True:
            return {
                "module_id": tool_id,
                "mode": "commanded",
                "running": True,
                "ok": False,
                "stage": "queue",
                "detail": queued,
            }
        response: dict[str, Any] | None = None
        deadline = time.monotonic() + self.MODULE_CLEANUP_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            response = await asyncio.to_thread(
                permission.tool_execution_response,
                tool_id,
                request_id,
            )
            if response and response.get("status") in {
                "completed",
                "failed",
                "cancelled",
            }:
                break
            await asyncio.sleep(1)
        if not response or response.get("status") != "completed":
            try:
                await toolbox.cancel_tool_execution(
                    {"tool_id": tool_id, "request_id": request_id}
                )
            except Exception:
                pass
            return {
                "module_id": tool_id,
                "mode": "commanded",
                "running": True,
                "ok": False,
                "stage": "execute",
                "error_code": "MODULE_CLEANUP_TIMEOUT",
                "detail": response,
            }
        tool_response = response.get("response")
        return {
            "module_id": tool_id,
            "mode": "commanded",
            "running": True,
            "ok": isinstance(tool_response, dict)
            and tool_response.get("ok") is True,
            "result": tool_response,
        }
