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

    async def _run_module_self_cleanup_sweep(
        self, byte_budget: int | None = None
    ) -> dict[str, Any]:
        """Collect per-module self-cleanup results for the daily cycle.

        Execution stays devolved: running governed tools receive the reserved
        ``toolbox_run_local_cleanup`` command on their own runtime; stopped
        tools contribute their persisted last-boot cleanup record; in-process
        modules (``main-system``) run the shared bounded cleanup directly.

        A533/A534: retired or disabled manifests are never commanded or
        swept — they are lineage evidence, not active modules.  Every
        in-process sweep is bounded by the remaining per-cycle byte quota
        and no more than ``MAX_MODULES_PER_CYCLE`` modules are processed.
        """

        from governance_rule.execution.tool_runtime.tool_local_cleanup import (
            read_local_cleanup_state,
            run_local_cleanup,
            write_local_cleanup_state,
        )

        started_at = self._iso_now()
        modules: list[dict[str, Any]] = []
        infrastructure_skipped_count = 0
        runtime_ready = self._runtime_ready()
        remaining_budget = (
            None if byte_budget is None else max(0, int(byte_budget))
        )
        module_cap = max(0, int(getattr(self, "MAX_MODULES_PER_CYCLE", 0) or 0))

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
                    run_local_cleanup,
                    module_id,
                    module_root,
                    max_cleaned_bytes=remaining_budget,
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
            if remaining_budget is not None:
                remaining_budget = max(
                    0, remaining_budget - int(cleanup.get("cleaned_bytes") or 0)
                )
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
            if module_cap and len(modules) >= module_cap:
                modules.append(
                    {
                        "module_id": str(tool.get("id") or ""),
                        "mode": "deferred",
                        "running": False,
                        "ok": True,
                        "deferred": True,
                        "reason": "MODULE_CYCLE_CAP_REACHED",
                    }
                )
                continue
            tool_id = str(tool.get("id") or "").strip()
            if not tool_id or tool_id in self.EXCLUDED_MODULE_IDS:
                continue
            lifecycle = tool.get("lifecycle")
            if tool.get("enabled") is False or (
                isinstance(lifecycle, dict)
                and str(lifecycle.get("status") or "").strip().casefold()
                == "retired"
            ):
                # A533/A534: retired owners are never commanded or swept.
                continue
            if not self._is_sweepable_module(tool):
                # Resident services, companion tools and hidden internal
                # services declare their own lifecycle and are cleaned by
                # their owning runtime; commanding them as modules both
                # times out and misstates the module topology (A534).
                infrastructure_skipped_count += 1
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
            # A stopped module cannot refresh its devolved cleanup record: the
            # sweep defers it with explicit evidence instead of failing the
            # cycle.  Staleness stays visible in the record.
            modules.append(
                {
                    "module_id": tool_id,
                    "mode": "last-recorded",
                    "running": False,
                    "ok": True,
                    "deferred": True,
                    "recorded": last is not None,
                    "stale": stale,
                    "reason": (
                        "RUNTIME_NOT_RUNNING_STALE_RECORD"
                        if stale and last
                        else "RUNTIME_NOT_RUNNING_NO_RECORD"
                        if last is None
                        else "RUNTIME_NOT_RUNNING"
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
            "infrastructure_skipped_count": infrastructure_skipped_count,
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

    @staticmethod
    def _is_sweepable_module(tool: dict[str, Any]) -> bool:
        """True only for registered independent tool cards.

        A534: the sweep addresses the independent tool set plus the
        in-process main-system.  Resident services (shared-layer,
        xingcheng), companion tools (star-chat) and hidden internal
        services (system-rescue) have no module self-cleanup command and
        must never be commanded as tools.
        """

        if tool.get("resident_service") is True:
            return False
        # A default-off module may still declare that the global cleaner must
        # not command it (e.g. local-model keeps model/RAG state and has no
        # module self-cleanup command).
        if tool.get("sweep_exclusion") is True:
            return False
        if tool.get("hidden_from_toolbox") is True:
            return False
        if tool.get("companion_tool") is True:
            return False
        return tool.get("main_system_independent_tool") is True

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
