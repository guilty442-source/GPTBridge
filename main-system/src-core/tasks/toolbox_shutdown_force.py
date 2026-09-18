"""Tool force-close mixin (A185 split).

Contains the force_close_tool method extracted from ShutdownMixin.
"""
# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import asyncio
import json
import re
import time
from typing import Any, Callable, Dict

from managers.process_utils import terminate_process_tree

from .tool_lifecycle_budget import (
    TOOL_CLOSE_BUDGET_CODE,
    TOOL_CLOSE_BUDGET_SECONDS,
    budget_evidence,
    remaining_seconds,
)


class ForceCloseMixin:
    """Force-close tool process tree."""

    _maintenance_ready: bool
    _force_closed_tool_ids: set[str]
    _process_state_lock: object
    _cancelled_request_ids: set[str]
    _source_ui_processes: dict[str, Any]
    _source_ui_runtime_sessions: dict[str, Any]
    _source_runtime_environments: dict[str, Any]

    def _maintenance_not_ready_result(self, operation: str) -> Dict[str, Any]:
        raise NotImplementedError

    def _authorize_tool_lifecycle(self, tool_id: str, action: str) -> None:
        raise NotImplementedError

    async def _active_tool_process(self, tool_id: str) -> tuple[Any, Any, Any]:
        raise NotImplementedError

    async def _started_tool_process(self, tool_id: str) -> tuple[Any, Any]:
        raise NotImplementedError

    def _tool_directory_for_id(self, tool_id: str) -> Any:
        raise NotImplementedError

    def _resolve_executable_file(self, manifest: dict, tool_dir: Any) -> Any:
        raise NotImplementedError

    def _has_governed_source_runtime(self, manifest: dict) -> bool:
        raise NotImplementedError

    def _resolve_special_unpacked_entry(self, manifest: dict, tool_dir: Any) -> Any:
        raise NotImplementedError

    def _sweep_and_stop_source_tool_processes(self, *args: Any) -> tuple[set[int], list[int]]:
        raise NotImplementedError

    def _sweep_and_stop_tool_processes(self, *args: Any) -> tuple[set[int], list[int]]:
        raise NotImplementedError

    def _forget_missing_tool(self, tool_id: str) -> None:
        raise NotImplementedError

    async def update_status(self, tool_id: str, status: str) -> Dict[str, Any]:
        raise NotImplementedError

    def _force_close_authorization(
        self, tool_id: str, request_id: str
    ) -> Dict[str, Any] | None:
        """Validate tool identity and lifecycle authority (fail-closed)."""
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}
        try:
            self._authorize_tool_lifecycle(tool_id, "stop")
        except PermissionError as error:
            error_code = str(error) or "PERMISSION_DENIED"
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": error_code,
                "message": error_code,
            }
        return None

    async def _tracked_process_snapshot(
        self, tool_id: str
    ) -> tuple[list[str], list[Any]]:
        """Tracked request ids and live processes for one tool."""
        request_ids: list[str] = []
        candidates: list[Any] = []
        tracked_request_id, process, _kind = await self._active_tool_process(tool_id)
        started_request_id, started_process = await self._started_tool_process(tool_id)
        if tracked_request_id is not None:
            request_ids.append(tracked_request_id)
        if started_request_id is not None:
            request_ids.append(started_request_id)
        for candidate in (process, started_process):
            if candidate is not None and candidate.returncode is None:
                candidates.append(candidate)
        return request_ids, candidates

    async def _terminate_tracked_tool_processes(self, tool_id: str) -> set[int]:
        """Cancel tracked requests and terminate every known tool process."""
        force_closed_process_ids: set[int] = set()
        request_ids, candidates = await self._tracked_process_snapshot(tool_id)
        for request_id in request_ids:
            async with self._process_state_lock:
                self._cancelled_request_ids.add(request_id)
        source_ui_process = self._source_ui_processes.pop(tool_id, None)
        self._source_ui_runtime_sessions.pop(tool_id, None)
        self._source_runtime_environments.pop(tool_id, None)
        if source_ui_process is not None and source_ui_process.returncode is None:
            if source_ui_process.pid:
                force_closed_process_ids.add(int(source_ui_process.pid))
            await terminate_process_tree(source_ui_process)
        for candidate in candidates:
            if candidate.pid:
                force_closed_process_ids.add(int(candidate.pid))
            await terminate_process_tree(candidate)
            try:
                await asyncio.wait_for(candidate.wait(), timeout=1)
            except asyncio.TimeoutError:
                await terminate_process_tree(candidate)
        return force_closed_process_ids

    async def _run_bounded_sweep(
        self, tool_id: str, tool_dir: Any, deadline: float
    ) -> tuple[set[int], list[int]]:
        """One manifest-selected sweep bounded by the remaining close budget."""
        manifest = json.loads(
            (tool_dir / "manifest.json").read_text(encoding="utf-8")
        )
        executable_file = self._resolve_executable_file(manifest, tool_dir)
        if self._has_governed_source_runtime(manifest):
            source_entry = self._resolve_special_unpacked_entry(manifest, tool_dir)
            sweep: Callable[..., Any] = self._sweep_and_stop_source_tool_processes
            sweep_args: tuple[Any, ...] = (
                tool_id,
                tool_dir,
                source_entry,
                executable_file,
            )
        else:
            sweep = self._sweep_and_stop_tool_processes
            sweep_args = (tool_id, tool_dir, executable_file)
        return await asyncio.wait_for(
            asyncio.to_thread(sweep, *sweep_args),
            timeout=max(0.1, remaining_seconds(deadline)),
        )

    async def _close_failure(
        self,
        tool_id: str,
        request_id: str,
        started: float,
        error_code: str,
        message: str,
        *,
        remaining_process_ids: list[int] | None = None,
    ) -> Dict[str, Any]:
        """Typed fail-closed close verdict with budget evidence."""
        await self.update_status(tool_id, "running")
        return {
            "ok": False,
            "tool_id": tool_id,
            "request_id": request_id,
            "error_code": error_code,
            "remaining_process_ids": list(remaining_process_ids or []),
            "message": message,
            **budget_evidence(started, TOOL_CLOSE_BUDGET_SECONDS),
        }

    def _removed_tool_result(
        self, tool_id: str, request_id: str, started: float
    ) -> Dict[str, Any]:
        self._forget_missing_tool(tool_id)
        return {
            "ok": True,
            "tool_id": tool_id,
            "request_id": request_id,
            "message": "Tool is missing; treated as removed",
            "force_closed": True,
            "removed": True,
            **budget_evidence(started, TOOL_CLOSE_BUDGET_SECONDS),
        }

    def _close_success_result(
        self,
        tool_id: str,
        request_id: str,
        started: float,
        force_closed_process_ids: set[int],
    ) -> Dict[str, Any]:
        return {
            "ok": True,
            "tool_id": tool_id,
            "request_id": request_id,
            "force_closed": True,
            "force_closed_process_ids": sorted(force_closed_process_ids),
            "remaining_process_ids": [],
            "message": "Tool process tree force-closed; no background process remains",
            **budget_evidence(started, TOOL_CLOSE_BUDGET_SECONDS),
        }

    async def _close_sweep_verdict(
        self,
        tool_id: str,
        request_id: str,
        started: float,
        deadline: float,
        tool_dir: Any,
    ) -> tuple[set[int], Dict[str, Any] | None]:
        """Run the bounded sweep; returns (stopped ids, failure verdict|None)."""
        try:
            stopped_ids, remaining_process_ids = await self._run_bounded_sweep(
                tool_id, tool_dir, deadline
            )
        except asyncio.TimeoutError:
            return set(), await self._close_failure(
                tool_id,
                request_id,
                started,
                TOOL_CLOSE_BUDGET_CODE,
                "Tool close exceeded the five-second budget",
            )
        except Exception as error:
            return set(), await self._close_failure(
                tool_id,
                request_id,
                started,
                "FORCE_CLOSE_FAILED",
                "Forced close could not verify the tool process state: "
                f"{type(error).__name__}",
            )
        if remaining_process_ids:
            return stopped_ids, await self._close_failure(
                tool_id,
                request_id,
                started,
                "FORCE_CLOSE_FAILED",
                "Tool process remains after forced close",
                remaining_process_ids=remaining_process_ids,
            )
        return stopped_ids, None

    async def force_close_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Force-close the complete tool process tree within the close budget."""
        if not self._maintenance_ready:
            return self._maintenance_not_ready_result("force_close_tool")
        started = time.monotonic()
        deadline = started + TOOL_CLOSE_BUDGET_SECONDS
        tool_id = str(payload.get("tool_id", "")).strip()
        command_request_id = str(payload.get("request_id") or "").strip()

        # Check if tool is independent - if so, refuse force-close from main system
        if self._is_independent_tool(tool_id):
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": command_request_id,
                "error_code": "INDEPENDENT_TOOL_PROTECTED",
                "message": "Independent tool lifecycle is managed separately; main system cannot force-close it",
            }

        error = self._force_close_authorization(tool_id, command_request_id)
        if error is not None:
            return error
        try:
            from core_system.tool_isolation import get_isolation_manager

            get_isolation_manager(self.project_root).mark_expected_stop(tool_id)
        except Exception:
            pass
        self._force_closed_tool_ids.add(tool_id)
        force_closed_process_ids = await self._terminate_tracked_tool_processes(
            tool_id
        )
        try:
            tool_dir = self._tool_directory_for_id(tool_id)
        except ValueError as error:
            return failure(str(error), "INVALID_TOOL_PATH")
        if not (tool_dir / "manifest.json").exists():
            return self._removed_tool_result(tool_id, command_request_id, started)
        stopped_ids, failure_verdict = await self._close_sweep_verdict(
            tool_id, command_request_id, started, deadline, tool_dir
        )
        if failure_verdict is not None:
            return failure_verdict
        force_closed_process_ids.update(stopped_ids)
        status_result = await self.update_status(tool_id, "stopped")
        if not status_result.get("ok"):
            return status_result
        return self._close_success_result(
            tool_id, command_request_id, started, force_closed_process_ids
        )

    def _is_independent_tool(self, tool_id: str) -> bool:
        """Check if a tool is declared as main-system independent."""
        try:
            tool_dir = self._tool_directory_for_id(tool_id)
            manifest_path = tool_dir / "manifest.json"
            if not manifest_path.exists():
                return False
            import json
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            return bool(manifest.get("main_system_independent_tool") is True)
        except Exception:
            return False


def failure(message: str, code: str) -> Dict[str, Any]:
    return {"ok": False, "error_code": code, "message": message}


__all__ = ["ForceCloseMixin"]
