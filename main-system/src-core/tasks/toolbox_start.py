"""Tool start orchestration and post-start process watching.

This module provides the StartMixin class.  Implementation details
live in submodules:

  * :mod:`tasks.toolbox_start_validation` — validation and runtime resolution.
  * :mod:`tasks.toolbox_start_spawn` — process spawning and finalization.
"""
# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import asyncio
from typing import Any, Dict

from .toolbox_watcher import ToolWatcherMixin
from .toolbox_start_validation import StartValidationMixin
from .toolbox_start_spawn import StartSpawnMixin


class StartMixin(StartValidationMixin, StartSpawnMixin, ToolWatcherMixin):
    """Tool start orchestration and post-start process lifecycle watching."""

    async def start_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        background = payload.get("background") is True
        repair_attempted = payload.get("_auto_repair_attempted") is True
        fallback_attempted = payload.get("_source_fallback_attempted") is True
        executable_fallback_attempted = (
            payload.get("_executable_fallback_attempted") is True
        )
        requested_mode = str(payload.get("runtime_mode") or "").strip().casefold()

        # Phase 1: validate request
        error = await asyncio.to_thread(self._validate_start_request, payload)
        if error is not None:
            return error
        tool_id = str(payload.get("tool_id", "")).strip()

        # Phase 2: resolve context (request_id, args, tool_dir, manifest)
        error = await asyncio.to_thread(
            self._resolve_start_context, payload, tool_id
        )
        if error is not None:
            return error
        ctx = self._start_ctx
        request_id = ctx["request_id"]
        args = ctx["args"]
        tool_dir = ctx["tool_dir"]
        manifest = ctx["manifest"]

        # Phase 3: handle runtime owner
        runtime_owner_tool_id = self._runtime_owner_tool_id(tool_id, manifest)
        if runtime_owner_tool_id != tool_id:
            return await self._start_runtime_owner(
                payload, tool_id, request_id, runtime_owner_tool_id,
                background, manifest, tool_dir,
            )

        # Phase 4: resolve runtime path
        error = await self._resolve_runtime_path(
            tool_id, request_id, manifest, tool_dir,
            background, requested_mode, repair_attempted,
            fallback_attempted, payload,
        )
        if error is not None:
            return error
        ctx = self._start_ctx
        executable_file = ctx["executable_file"]
        runtime_path = ctx["runtime_path"]
        runtime_mode = ctx["runtime_mode"]
        use_source_runtime = ctx["use_source_runtime"]

        # Phase 5: verify package and contract
        error = await self._verify_package_and_contract(
            tool_id, request_id, manifest, payload, requested_mode,
            repair_attempted, fallback_attempted,
        )
        if error is not None:
            return error
        ctx = self._start_ctx
        use_source_runtime = ctx["use_source_runtime"]
        runtime_path = ctx["runtime_path"]
        runtime_mode = ctx["runtime_mode"]

        # Phase 6: handle existing running process
        existing = await self._handle_existing_process(
            tool_id, request_id, background, manifest, tool_dir,
            executable_file, runtime_path, runtime_mode,
        )
        if existing is not None:
            return existing

        # Phase 7: reserve process slot
        reservation_error = await self._reserve_tool_process(
            request_id=request_id, tool_id=tool_id, kind="started",
        )
        if reservation_error is not None:
            return reservation_error

        # Phase 8: handle running process IDs
        running = await self._handle_running_process_ids(
            tool_id, request_id, background, manifest, tool_dir,
            executable_file, runtime_path, runtime_mode,
        )
        if running is not None:
            return running

        # Phase 9: spawn process
        spawn_result = await self._spawn_tool_process(
            tool_id, request_id, manifest, tool_dir, executable_file, args,
            background, requested_mode, repair_attempted,
            fallback_attempted, executable_fallback_attempted, payload,
        )
        if isinstance(spawn_result, dict):
            return spawn_result
        process = spawn_result

        # Phase 10: validate spawned process
        validation_error = await self._validate_spawned_process(
            tool_id, request_id, process, payload, tool_dir, manifest,
            repair_attempted,
        )
        if validation_error is not None:
            return validation_error

        # Phase 11: finalize
        return await self._finalize_tool_start(
            request_id, tool_id, runtime_path, runtime_mode, process,
            background, manifest, tool_dir, executable_file,
            requested_mode, repair_attempted,
            executable_fallback_attempted, payload,
        )

    async def _start_runtime_owner(
        self, payload: Dict[str, Any], tool_id: str, request_id: str,
        runtime_owner_tool_id: str, background: bool, manifest: dict,
        tool_dir: Any,
    ) -> Dict[str, Any]:
        """Start the shared runtime owner and connect companion tool."""
        owner_result = await self.start_tool(
            {
                "tool_id": runtime_owner_tool_id,
                "request_id": f"{request_id}:owner",
                "runtime_mode": "source",
                "background": True,
            }
        )
        if owner_result.get("ok") is not True:
            return {
                "ok": False, "tool_id": tool_id, "request_id": request_id,
                "error_code": "RUNTIME_OWNER_NOT_READY",
                "message": "Shared owner runtime could not be started",
                "owner_result": owner_result,
            }
        runtime_environment = self._source_runtime_environments.get(
            runtime_owner_tool_id
        )
        if runtime_environment is None:
            return {
                "ok": False, "tool_id": tool_id, "request_id": request_id,
                "error_code": "RUNTIME_OWNER_NOT_READY",
                "message": "Shared owner runtime session is unavailable",
            }
        ui_result: dict[str, Any] = {}
        if not background and manifest.get("has_custom_ui") is True:
            ui_result = await self._launch_source_ui(
                tool_id, tool_dir, manifest, runtime_environment,
                runtime_tool_id=runtime_owner_tool_id,
            )
            if ui_result.get("ok") is not True:
                return {
                    "tool_id": tool_id, "request_id": request_id, **ui_result,
                }
        await self.update_status(tool_id, "running")
        return {
            "ok": True, "tool_id": tool_id, "request_id": request_id,
            "runtime_owner_tool_id": runtime_owner_tool_id,
            "runtime_mode": "shared-owner-runtime",
            "background": background,
            "message": "Companion interface connected to its shared owner runtime",
            **ui_result,
        }
