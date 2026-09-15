"""Tool force-close mixin (A185 split).

Contains the force_close_tool method extracted from ShutdownMixin.
"""
# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Dict

from managers.process_utils import terminate_process_tree


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

    async def force_close_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Force-close the complete tool process tree and verify no process remains."""

        if not self._maintenance_ready:
            return self._maintenance_not_ready_result("force_close_tool")
        tool_id = str(payload.get("tool_id", "")).strip()
        command_request_id = str(payload.get("request_id") or "").strip()
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
                "request_id": command_request_id,
                "error_code": error_code,
                "message": error_code,
            }

        self._force_closed_tool_ids.add(tool_id)

        tracked_request_id, process, _kind = await self._active_tool_process(tool_id)
        if tracked_request_id is not None:
            async with self._process_state_lock:
                self._cancelled_request_ids.add(tracked_request_id)

        started_request_id, started_process = await self._started_tool_process(tool_id)
        if started_request_id is not None:
            async with self._process_state_lock:
                self._cancelled_request_ids.add(started_request_id)

        tracked_processes = {
            id(candidate): candidate
            for candidate in (process, started_process)
            if candidate is not None and candidate.returncode is None
        }
        force_closed_process_ids: set[int] = set()
        source_ui_process = self._source_ui_processes.pop(tool_id, None)
        self._source_ui_runtime_sessions.pop(tool_id, None)
        self._source_runtime_environments.pop(tool_id, None)
        if source_ui_process is not None and source_ui_process.returncode is None:
            if source_ui_process.pid:
                force_closed_process_ids.add(int(source_ui_process.pid))
            await terminate_process_tree(source_ui_process)
        for candidate in tracked_processes.values():
            if candidate.pid:
                force_closed_process_ids.add(int(candidate.pid))
            await terminate_process_tree(candidate)
            try:
                await asyncio.wait_for(candidate.wait(), timeout=1)
            except asyncio.TimeoutError:
                await terminate_process_tree(candidate)

        try:
            tool_dir = self._tool_directory_for_id(tool_id)
        except ValueError as error:
            return failure(str(error), "INVALID_TOOL_PATH")
        manifest_path = tool_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                executable_file = self._resolve_executable_file(manifest, tool_dir)
                if self._has_governed_source_runtime(manifest):
                    source_entry = self._resolve_special_unpacked_entry(
                        manifest,
                        tool_dir,
                    )
                    stopped_ids, remaining_process_ids = await asyncio.to_thread(
                        self._sweep_and_stop_source_tool_processes,
                        tool_id,
                        tool_dir,
                        source_entry,
                        executable_file,
                    )
                else:
                    stopped_ids, remaining_process_ids = await asyncio.to_thread(
                        self._sweep_and_stop_tool_processes,
                        tool_id,
                        tool_dir,
                        executable_file,
                    )
                force_closed_process_ids.update(stopped_ids)
            except Exception as error:
                await self.update_status(tool_id, "running")
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": command_request_id,
                    "error_code": "FORCE_CLOSE_FAILED",
                    "remaining_process_ids": [],
                    "message": (
                        "Forced close could not verify the tool process state: "
                        f"{type(error).__name__}"
                    ),
                }
        else:
            self._forget_missing_tool(tool_id)
            return {
                "ok": True,
                "tool_id": tool_id,
                "request_id": command_request_id,
                "message": "舊應用程式資料已移除。",
                "force_closed": True,
                "removed": True,
            }

        if remaining_process_ids:
            await self.update_status(tool_id, "running")
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": command_request_id,
                "error_code": "FORCE_CLOSE_FAILED",
                "remaining_process_ids": remaining_process_ids,
                "message": "Tool process remains after forced close",
            }

        status_result = await self.update_status(tool_id, "stopped")
        if not status_result.get("ok"):
            return status_result
        return {
            "ok": True,
            "tool_id": tool_id,
            "request_id": command_request_id,
            "force_closed": True,
            "force_closed_process_ids": sorted(force_closed_process_ids),
            "remaining_process_ids": [],
            "message": "Tool process tree force-closed; no background process remains",
        }


def failure(message: str, code: str) -> Dict[str, Any]:
    return {"ok": False, "error_code": code, "message": message}


__all__ = ["ForceCloseMixin"]
