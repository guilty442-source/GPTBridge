"""Tool start process spawning and finalization mixin.

Provides process spawning, existing-process handling, post-spawn
validation, and registration phases for the tool start lifecycle.

Windows background subprocess no-window flag: CREATE_NO_WINDOW.
"""

from __future__ import annotations

import asyncio
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict

from managers.process_utils import terminate_process_tree

from core_system.tool_isolation import get_isolation_manager
from .toolbox_start_spawn_process import SpawnProcessMixin


class StartSpawnMixin(SpawnProcessMixin):
    """Process spawning and finalization methods for tool start."""

    async def _handle_existing_process(
        self, tool_id: str, request_id: str, background: bool,
        manifest: dict, tool_dir: Path, executable_file: Path,
        runtime_path: Path, runtime_mode: str,
    ) -> Dict[str, Any] | None:
        """Handle already-running tool process. Returns result dict or None to continue."""
        ctx = self._start_ctx
        use_source_runtime = ctx["use_source_runtime"]

        tracked_request_id, tracked_process = await self._started_tool_process(tool_id)
        if (
            tracked_request_id is not None
            and tracked_process is not None
            and tracked_process.returncode is None
        ):
            if background or use_source_runtime:
                ui_result: dict[str, Any] = {}
                if (
                    use_source_runtime
                    and not background
                    and manifest.get("has_custom_ui") is True
                ):
                    runtime_environment = self._source_runtime_environments.get(tool_id)
                    if runtime_environment is not None:
                        ui_result = await self._launch_source_ui(
                            tool_id, tool_dir, manifest, runtime_environment,
                        )
                status_result = await self.update_status(tool_id, "running")
                if not status_result.get("ok"):
                    return status_result
                return {
                    "ok": True, "tool_id": tool_id, "request_id": request_id,
                    "pid": tracked_process.pid,
                    "runtime_path": str(runtime_path),
                    "runtime_mode": runtime_mode,
                    "background": True,
                    "message": "Tool runtime is already running in background",
                    **ui_result,
                }
            activation = await self._activate_existing_tool_window(
                tool_id=tool_id, tool_dir=tool_dir,
                executable_file=executable_file, manifest=manifest,
            )
            status_result = await self.update_status(tool_id, "running")
            if not status_result.get("ok"):
                return status_result
            return {
                "ok": True, "tool_id": tool_id, "request_id": request_id,
                "pid": tracked_process.pid,
                "executable_path": str(executable_file),
                "message": "Tool executable is already running; activation requested",
                **activation,
            }
        return None

    async def _handle_running_process_ids(
        self, tool_id: str, request_id: str, background: bool,
        manifest: dict, tool_dir: Path, executable_file: Path,
        runtime_path: Path, runtime_mode: str,
    ) -> Dict[str, Any] | None:
        """Handle already-running process IDs. Returns result dict or None to continue."""
        ctx = self._start_ctx
        use_source_runtime = ctx["use_source_runtime"]
        source_entry = ctx["source_entry"]

        running_process_ids = (
            await asyncio.to_thread(
                self._running_source_runtime_process_ids, source_entry
            )
            if use_source_runtime and source_entry is not None
            else await asyncio.to_thread(
                self._running_executable_process_ids, executable_file
            )
        )
        if running_process_ids and use_source_runtime and source_entry is not None:
            try:
                get_isolation_manager(self.project_root).mark_expected_stop(tool_id)
            except Exception:
                pass
            await asyncio.to_thread(self._stop_running_source_runtime, source_entry)
            for _ in range(20):
                if not await asyncio.to_thread(
                    self._running_source_runtime_process_ids, source_entry
                ):
                    break
                await asyncio.sleep(0.1)
            running_process_ids = await asyncio.to_thread(
                self._running_source_runtime_process_ids, source_entry
            )
        if running_process_ids:
            await self._release_tool_process(request_id)
            if background or use_source_runtime:
                ui_result: dict[str, Any] = {}
                if (
                    use_source_runtime
                    and not background
                    and manifest.get("has_custom_ui") is True
                ):
                    runtime_environment = self._source_runtime_environments.get(tool_id)
                    if runtime_environment is not None:
                        ui_result = await self._launch_source_ui(
                            tool_id, tool_dir, manifest, runtime_environment,
                        )
                status_result = await self.update_status(tool_id, "running")
                if not status_result.get("ok"):
                    return status_result
                return {
                    "ok": True, "tool_id": tool_id, "request_id": request_id,
                    "pid": running_process_ids[0],
                    "runtime_path": str(runtime_path),
                    "runtime_mode": runtime_mode,
                    "background": True,
                    "message": "Tool runtime is already running in background",
                    **ui_result,
                }
            activation = await self._activate_existing_tool_window(
                tool_id=tool_id, tool_dir=tool_dir,
                executable_file=executable_file, manifest=manifest,
            )
            status_result = await self.update_status(tool_id, "running")
            if not status_result.get("ok"):
                return status_result
            return {
                "ok": True, "tool_id": tool_id, "request_id": request_id,
                "pid": running_process_ids[0],
                "executable_path": str(executable_file),
                "message": "Tool executable is already running; activation requested",
                **activation,
            }
        return None

    async def _validate_spawned_process(
        self, tool_id: str, request_id: str, process: Any,
        payload: Dict[str, Any], tool_dir: Path, manifest: dict,
        repair_attempted: bool,
    ) -> Dict[str, Any] | None:
        """Validate the spawned process survived startup. Returns error dict or None."""
        ctx = self._start_ctx
        use_source_runtime = ctx["use_source_runtime"]

        if use_source_runtime:
            for _ in range(3):
                if process.returncode is not None:
                    break
                await asyncio.sleep(0.1)
            if process.returncode is not None:
                await self._release_tool_process(request_id)
                await self.update_status(tool_id, "error")
                failure_result = {
                    "ok": False, "tool_id": tool_id, "request_id": request_id,
                    "error_code": "SOURCE_RUNTIME_EXITED",
                    "message": "Governed source runtime exited during startup",
                    "exit_code": process.returncode,
                }
                if not repair_attempted:
                    return await self._retry_start_after_central_repair(
                        payload, tool_id, tool_dir, manifest, failure_result,
                    )
                return failure_result
        return None

    async def _finalize_tool_start(
        self, request_id: str, tool_id: str, runtime_path: Path,
        runtime_mode: str, process: Any, background: bool,
        manifest: dict, tool_dir: Path, executable_file: Path,
        requested_mode: str, repair_attempted: bool,
        executable_fallback_attempted: bool, payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Register the process and finalize the start. Returns final result."""
        ctx = self._start_ctx
        use_source_runtime = ctx["use_source_runtime"]

        cancel_pending = await self._register_tool_process(request_id, process)
        try:
            isolation_mgr = get_isolation_manager(self.project_root)
            await asyncio.to_thread(isolation_mgr.register_tool, tool_id, process)
        except Exception:
            pass
        asyncio.create_task(
            self._watch_started_tool(
                request_id, tool_id, runtime_path, use_source_runtime, process,
                close_program_on_exit=(
                    manifest.get("has_custom_ui") is True and not background
                ),
            )
        )
        await self._release_started_tool_command_slot(request_id)
        if cancel_pending and process.returncode is None:
            await terminate_process_tree(process)
        status_result = await self.update_status(tool_id, "running")
        ui_result: dict[str, Any] = {}
        if (
            use_source_runtime
            and not background
            and manifest.get("has_custom_ui") is True
        ):
            runtime_environment = self._source_runtime_environments.get(tool_id)
            if runtime_environment is not None:
                ui_result = await self._launch_source_ui(
                    tool_id, tool_dir, manifest, runtime_environment,
                )
            else:
                ui_result = {
                    "ok": False,
                    "error_code": "SOURCE_UI_UNAVAILABLE",
                    "message": "Governed source UI session is unavailable",
                }
        if ui_result and not ui_result.get("ok") and not repair_attempted:
            failure_result = {
                "tool_id": tool_id, "request_id": request_id, **ui_result,
            }
            await self.force_close_tool(
                {
                    "tool_id": tool_id,
                    "request_id": f"central-repair-close-{uuid.uuid4().hex}",
                }
            )
            if (
                not executable_fallback_attempted
                and self._executable_fallback_allowed(
                    manifest, requested_mode, executable_file.exists(),
                )
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "executable"
                fallback_payload["_executable_fallback_attempted"] = True
                fallback_payload["_source_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            return await self._retry_start_after_central_repair(
                payload, tool_id, tool_dir, manifest, failure_result,
            )
        if not status_result.get("ok"):
            return {
                "ok": True, "tool_id": tool_id, "request_id": request_id,
                "pid": process.pid, "runtime_path": str(runtime_path),
                "runtime_mode": runtime_mode,
                "status_warning": str(status_result.get("message", "status update failed")),
                "message": "Tool runtime started", **ui_result,
            }
        return {
            "ok": True, "tool_id": tool_id, "request_id": request_id,
            "pid": process.pid, "runtime_path": str(runtime_path),
            "runtime_mode": runtime_mode, "background": background,
            "message": "Tool runtime started", **ui_result,
        }
