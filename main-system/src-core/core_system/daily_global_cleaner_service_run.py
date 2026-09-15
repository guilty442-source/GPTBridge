"""Daily global cleaner run mixin (A185 split).

Contains the run_if_due method extracted from DailyGlobalCleanerService.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any


class DailyGlobalCleanerRunMixin:
    """Daily global cleaner run-if-due execution."""

    app: Any
    _run_lock: object

    def is_due(self) -> bool:
        raise NotImplementedError

    def _load_state(self) -> dict[str, Any]:
        raise NotImplementedError

    def _save_state(self, state: dict[str, Any]) -> None:
        raise NotImplementedError

    def _iso_now(self) -> str:
        raise NotImplementedError

    def _permission_master_entry(self) -> Any:
        raise NotImplementedError

    async def _run_module_self_cleanup_sweep(self) -> dict[str, Any]:
        raise NotImplementedError

    async def run_if_due(self, *, force: bool = False) -> dict[str, Any]:
        async with self._run_lock:
            if not force and not self.is_due():
                return {"ok": True, "skipped": True, "reason": "NOT_DUE"}
            request_id = f"daily-global-cleaner-{time.time_ns()}"
            state = self._load_state()
            state.update(
                {
                    "last_started_epoch": time.time(),
                    "last_started_at": self._iso_now(),
                    "last_request_id": request_id,
                    "last_ok": False,
                    "last_status": "starting",
                }
            )
            state.pop("last_error", None)
            toolbox = self.app.toolbox_service
            permission = self._permission_master_entry()
            if toolbox is None or permission is None:
                error = {
                    "ok": False,
                    "error_code": "GOVERNED_RUNTIME_UNAVAILABLE",
                }
                state.update(
                    {
                        "last_status": "governed_runtime_unavailable",
                        "last_completed_at": self._iso_now(),
                        "last_error": error,
                    }
                )
                self._save_state(state)
                return error
            self._save_state(state)
            try:
                start_result = await toolbox.start_tool(
                    {
                        "tool_id": "global-cleaner",
                        "request_id": f"start-{request_id}",
                        "background": True,
                        "runtime_mode": "source",
                    }
                )
            except Exception as error:
                start_result = {
                    "ok": False,
                    "error_code": "GLOBAL_CLEANER_START_EXCEPTION",
                    "message": f"{type(error).__name__}: {error}",
                }
            if start_result.get("ok") is not True:
                state.update(
                    {
                        "last_status": "start_failed",
                        "last_completed_at": self._iso_now(),
                        "last_error": start_result,
                    }
                )
                self._save_state(state)
                return {"ok": False, "stage": "start", "detail": start_result}
            started_here = "already running" not in str(
                start_result.get("message") or ""
            ).casefold()
            response: dict[str, Any] | None = None
            try:
                queued = await toolbox.request_tool_execution(
                    {
                        "tool_id": "global-cleaner",
                        "request_id": request_id,
                        "_governed_command": "toolbox_request_tool_execution",
                        "args": ["--governed-daily-maintenance", "--json"],
                    }
                )
                if queued.get("ok") is not True:
                    return {"ok": False, "stage": "queue", "detail": queued}
                deadline = time.monotonic() + self.RESPONSE_TIMEOUT_SECONDS
                while time.monotonic() < deadline:
                    response = await asyncio.to_thread(
                        permission.tool_execution_response,
                        "global-cleaner",
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
                    return {
                        "ok": False,
                        "stage": "execute",
                        "error_code": "GLOBAL_CLEANER_TIMEOUT",
                        "detail": response,
                    }
                tool_response = response.get("response")
                ok = isinstance(tool_response, dict) and tool_response.get("ok") is True
                return {
                    "ok": ok,
                    "stage": "completed",
                    "request_id": request_id,
                    "detail": tool_response,
                }
            finally:
                close_result = (
                    await toolbox.force_close_tool(
                        {
                            "tool_id": "global-cleaner",
                            "request_id": f"stop-{request_id}",
                        }
                    )
                    if started_here
                    else {
                        "ok": True,
                        "delegated": True,
                        "reason": "PREEXISTING_GLOBAL_CLEANER_RETAINED",
                    }
                )
                result_ok = bool(
                    response
                    and response.get("status") == "completed"
                    and isinstance(response.get("response"), dict)
                    and response["response"].get("ok") is True
                )
                state.update(
                    {
                        "last_completed_at": self._iso_now(),
                        "last_ok": result_ok,
                        "last_status": "completed" if result_ok else "failed",
                        "last_response": response,
                        "last_close_result": close_result,
                    }
                )
                self._save_state(state)
                try:
                    state["module_cleanup"] = (
                        await self._run_module_self_cleanup_sweep()
                    )
                    self._save_state(state)
                except Exception:
                    pass


__all__ = ["DailyGlobalCleanerRunMixin"]
