"""Tool start orchestration and post-start process watching."""
from __future__ import annotations

import asyncio
import json
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict

from managers.process_utils import terminate_process_tree

from governance_rule.execution.integrity.package_integrity import (
    load_package_metadata,
    verify_packaged_app,
)

from .toolbox_constants import _background_subprocess_kwargs


class StartMixin:
    """Tool start orchestration and post-start process lifecycle watching."""

    async def start_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self._maintenance_ready:
            return self._maintenance_not_ready_result("start_tool")
        tool_id = str(payload.get("tool_id", "")).strip()
        background = payload.get("background") is True
        managed_restart = payload.get("_managed_restart") is True
        repair_attempted = payload.get("_auto_repair_attempted") is True
        fallback_attempted = payload.get("_source_fallback_attempted") is True
        executable_fallback_attempted = (
            payload.get("_executable_fallback_attempted") is True
        )
        requested_mode = str(payload.get("runtime_mode") or "").strip().casefold()
        if not tool_id:
            return {"ok": False, "message": "Missing tool_id"}
        if not re.match(r"^[a-z0-9_-]+$", tool_id):
            return {"ok": False, "message": "Invalid tool_id format"}
        try:
            self._authorize_tool_lifecycle(tool_id, "start")
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        if not managed_restart:
            self._force_closed_tool_ids.discard(tool_id)
            self._background_restart_attempts.pop(tool_id, None)

        request_id, request_error = self._tool_request_id(payload)
        if request_error is not None or request_id is None:
            return {"tool_id": tool_id, **(request_error or {})}
        args, argument_error = self._tool_arguments(payload, tool_id)
        if argument_error is not None or args is None:
            if argument_error is not None:
                argument_error["request_id"] = request_id
            return argument_error or {"ok": False, "tool_id": tool_id, "request_id": request_id}

        try:
            tool_dir = self._tool_directory_for_id(tool_id)
        except ValueError as error:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "INVALID_TOOL_PATH",
                "message": str(error),
            }
        manifest_path = tool_dir / "manifest.json"
        if not manifest_path.exists():
            self._forget_missing_tool(tool_id)
            return {**self._missing_tool_result(tool_id), "request_id": request_id}

        try:
            manifest, _cached_dir = self._load_manifest_cached(tool_id)
        except Exception as exc:
            return {"ok": False, "tool_id": tool_id, "message": f"Invalid tool manifest: {exc}"}

        if manifest.get("enabled", True) is False:
            return {"ok": False, "tool_id": tool_id, "message": "Tool is disabled"}

        runtime_owner_tool_id = self._runtime_owner_tool_id(tool_id, manifest)
        if runtime_owner_tool_id != tool_id:
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
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "error_code": "RUNTIME_OWNER_NOT_READY",
                    "message": "Shared owner runtime could not be started",
                    "owner_result": owner_result,
                }
            runtime_environment = self._source_runtime_environments.get(
                runtime_owner_tool_id
            )
            if runtime_environment is None:
                return {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "error_code": "RUNTIME_OWNER_NOT_READY",
                    "message": "Shared owner runtime session is unavailable",
                }
            ui_result: dict[str, Any] = {}
            if not background and manifest.get("has_custom_ui") is True:
                ui_result = await self._launch_source_ui(
                    tool_id,
                    tool_dir,
                    manifest,
                    runtime_environment,
                    runtime_tool_id=runtime_owner_tool_id,
                )
                if ui_result.get("ok") is not True:
                    return {
                        "tool_id": tool_id,
                        "request_id": request_id,
                        **ui_result,
                    }
            await self.update_status(tool_id, "running")
            return {
                "ok": True,
                "tool_id": tool_id,
                "request_id": request_id,
                "runtime_owner_tool_id": runtime_owner_tool_id,
                "runtime_mode": "shared-owner-runtime",
                "background": background,
                "message": "Companion interface connected to its shared owner runtime",
                **ui_result,
            }

        version_failure = self._tool_version_failure(tool_id, request_id, manifest)
        if version_failure is not None:
            await self.update_status(tool_id, "error")
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload,
                    tool_id,
                    tool_dir,
                    manifest,
                    version_failure,
                )
            return version_failure

        source_runtime = self._has_governed_source_runtime(manifest)
        source_entry: Path | None = None
        python_executable: Path | None = None
        try:
            executable_file = self._resolve_executable_file(manifest, tool_dir)
            if source_runtime:
                source_entry = self._resolve_special_unpacked_entry(
                    manifest,
                    tool_dir,
                )
                python_executable = self._resolve_python_executable(
                    manifest,
                    tool_dir,
                )
        except ValueError as error:
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": (
                    "INVALID_SOURCE_RUNTIME"
                    if source_runtime
                    else "INVALID_EXECUTABLE_PATH"
                ),
                "message": str(error),
            }
        use_source_runtime = self._source_launch_requested(
            manifest,
            background=background,
            requested_mode=requested_mode,
            executable_exists=executable_file.exists(),
        )
        # Independent tools must launch from governed native source code;
        # packaged EXE startup is no longer supported.
        if not source_runtime:
            await self.update_status(tool_id, "stopped")
            return {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "GOVERNED_SOURCE_RUNTIME_REQUIRED",
                "message": "Independent tools must launch from governed source code; EXE launch is disabled",
            }
        runtime_path = source_entry if use_source_runtime else executable_file
        runtime_mode = "governed-source" if use_source_runtime else "executable"
        if not use_source_runtime and not executable_file.exists():
            await self.update_status(tool_id, "stopped")
            failure_result = {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "EXECUTABLE_MISSING",
                "message": f"Standalone EXE not found. Run npm run package:tool -- {tool_id}",
                "executable_path": str(executable_file),
            }
            if (
                not fallback_attempted
                and self._source_fallback_allowed(manifest, requested_mode)
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload,
                    tool_id,
                    tool_dir,
                    manifest,
                    failure_result,
                )
            return failure_result

        package_check = (
            {"ok": True}
            if use_source_runtime
            else verify_packaged_app(
                executable_file.parent / "resources" / "app",
            )
        )
        if not package_check.get("ok"):
            await self.update_status(tool_id, "error")
            failure_result = {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "PACKAGE_UNVERIFIED",
                "package_error_code": str(package_check.get("error_code") or ""),
                "message": (
                    f"{package_check.get('message', 'Package verification failed')}. "
                    f"Run npm run package:tool -- {tool_id}"
                ),
                "executable_path": str(executable_file),
            }
            if (
                not fallback_attempted
                and self._source_fallback_allowed(manifest, requested_mode)
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload,
                    tool_id,
                    tool_dir,
                    manifest,
                    failure_result,
                )
            if self._source_fallback_allowed(manifest, requested_mode):
                use_source_runtime = True
                runtime_path = source_entry
                runtime_mode = "governed-source"
                package_check = {"ok": True}
            else:
                return failure_result
        package_metadata = (
            {}
            if use_source_runtime
            else load_package_metadata(
                executable_file.parent / "resources" / "app"
            )
        )
        source_version = str(manifest.get("version") or "").strip()
        packaged_version = str(
            package_metadata.get("tool_version") or source_version
        ).strip()
        if (
            not use_source_runtime
            and (not source_version or packaged_version != source_version)
        ):
            await self.update_status(tool_id, "error")
            failure_result = {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "STALE_TOOL_PACKAGE",
                "message": (
                    "This tool's manifest version changed after its EXE was packaged. "
                    f"Run npm run package:tool -- {tool_id}"
                ),
                "executable_path": str(executable_file),
            }
            if (
                not fallback_attempted
                and self._source_fallback_allowed(manifest, requested_mode)
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload,
                    tool_id,
                    tool_dir,
                    manifest,
                    failure_result,
                )
            if self._source_fallback_allowed(manifest, requested_mode):
                use_source_runtime = True
                runtime_path = source_entry
                runtime_mode = "governed-source"
                package_metadata = {}
            else:
                return failure_result
        try:
            contract = json.loads(
                (
                    self.project_root
                    / "main-system"
                    / "config"
                    / "tool-runtime-contract.json"
                ).read_text(encoding="utf-8")
            )
            current_contract = int(contract["contract_version"])
            minimum_contract = int(
                contract["minimum_supported_contract_version"]
            )
            packaged_contract = int(
                package_metadata.get("runtime_contract_version") or 1
            )
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            current_contract = 1
            minimum_contract = 1
            packaged_contract = int(
                package_metadata.get("runtime_contract_version") or 1
            )
        if (
            not use_source_runtime
            and not minimum_contract <= packaged_contract <= current_contract
        ):
            await self.update_status(tool_id, "error")
            failure_result = {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "INCOMPATIBLE_TOOL_RUNTIME",
                "message": (
                    "The packaged tool runtime contract is incompatible with "
                    "this GPTBridge version."
                ),
                "executable_path": str(executable_file),
            }
            if (
                not fallback_attempted
                and self._source_fallback_allowed(manifest, requested_mode)
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload,
                    tool_id,
                    tool_dir,
                    manifest,
                    failure_result,
                )
            if self._source_fallback_allowed(manifest, requested_mode):
                use_source_runtime = True
                runtime_path = source_entry
                runtime_mode = "governed-source"
            else:
                return failure_result

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
                            tool_id,
                            tool_dir,
                            manifest,
                            runtime_environment,
                        )
                status_result = await self.update_status(tool_id, "running")
                if not status_result.get("ok"):
                    return status_result
                return {
                    "ok": True,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "pid": tracked_process.pid,
                    "runtime_path": str(runtime_path),
                    "runtime_mode": runtime_mode,
                    "background": True,
                    "message": "Tool runtime is already running in background",
                    **ui_result,
                }
            activation = await self._activate_existing_tool_window(
                tool_id=tool_id,
                tool_dir=tool_dir,
                executable_file=executable_file,
                manifest=manifest,
            )
            status_result = await self.update_status(tool_id, "running")
            if not status_result.get("ok"):
                return status_result
            return {
                "ok": True,
                "tool_id": tool_id,
                "request_id": request_id,
                "pid": tracked_process.pid,
                "executable_path": str(executable_file),
                "message": "Tool executable is already running; activation requested",
                **activation,
            }

        reservation_error = await self._reserve_tool_process(
            request_id=request_id,
            tool_id=tool_id,
            kind="started",
        )
        if reservation_error is not None:
            return reservation_error

        running_process_ids = (
            self._running_source_runtime_process_ids(source_entry)
            if use_source_runtime and source_entry is not None
            else self._running_executable_process_ids(executable_file)
        )
        if running_process_ids and use_source_runtime and source_entry is not None:
            # A source runtime left behind by an earlier main-system generation
            # cannot be supervised or restarted by this ToolboxService instance.
            # Replace it with a freshly governed, owned process.
            await asyncio.to_thread(self._stop_running_source_runtime, source_entry)
            for _ in range(20):
                if not self._running_source_runtime_process_ids(source_entry):
                    break
                await asyncio.sleep(0.1)
            running_process_ids = self._running_source_runtime_process_ids(source_entry)
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
                            tool_id,
                            tool_dir,
                            manifest,
                            runtime_environment,
                        )
                status_result = await self.update_status(tool_id, "running")
                if not status_result.get("ok"):
                    return status_result
                return {
                    "ok": True,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "pid": running_process_ids[0],
                    "runtime_path": str(runtime_path),
                    "runtime_mode": runtime_mode,
                    "background": True,
                    "message": "Tool runtime is already running in background",
                    **ui_result,
                }
            activation = await self._activate_existing_tool_window(
                tool_id=tool_id,
                tool_dir=tool_dir,
                executable_file=executable_file,
                manifest=manifest,
            )
            status_result = await self.update_status(tool_id, "running")
            if not status_result.get("ok"):
                return status_result
            return {
                "ok": True,
                "tool_id": tool_id,
                "request_id": request_id,
                "pid": running_process_ids[0],
                "executable_path": str(executable_file),
                "message": "Tool executable is already running; activation requested",
                **activation,
            }

        try:
            if use_source_runtime and source_entry is not None and python_executable is not None:
                source_environment = self._source_runtime_environment(
                    tool_id,
                    tool_dir,
                    manifest,
                )
                process = await asyncio.create_subprocess_exec(
                    str(python_executable),
                    "-B",
                    "-s",
                    "-E",
                    "-X",
                    "utf8",
                    str(source_entry),
                    *args,
                    cwd=str(tool_dir),
                    stdout=subprocess.DEVNULL,
                    # Preserve governed runtime failures in the main backend
                    # diagnostic stream so automatic repair can classify an
                    # immediate-exit failure instead of reporting a false start.
                    stderr=None,
                    env=source_environment,
                    **_background_subprocess_kwargs(),
                )
                self._source_runtime_environments[tool_id] = source_environment
            else:
                process = await asyncio.create_subprocess_exec(
                    str(executable_file),
                    *args,
                    cwd=str(tool_dir),
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=self._tool_environment(
                        tool_id,
                        tool_dir,
                        manifest,
                        start_hidden=background,
                    ),
                    **_background_subprocess_kwargs(),
                )
        except Exception as exc:
            await self._release_tool_process(request_id)
            await self.update_status(tool_id, "error")
            failure_result = {
                "ok": False,
                "tool_id": tool_id,
                "request_id": request_id,
                "error_code": "PROCESS_START_FAILED",
                "message": str(exc),
            }
            if (
                use_source_runtime
                and not executable_fallback_attempted
                and self._executable_fallback_allowed(
                    manifest,
                    requested_mode,
                    executable_file.exists(),
                )
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "executable"
                fallback_payload["_executable_fallback_attempted"] = True
                fallback_payload["_source_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if (
                not use_source_runtime
                and not fallback_attempted
                and self._source_fallback_allowed(manifest, requested_mode)
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "source"
                fallback_payload["_source_fallback_attempted"] = True
                fallback_payload["_executable_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            if not repair_attempted:
                return await self._retry_start_after_central_repair(
                    payload,
                    tool_id,
                    tool_dir,
                    manifest,
                    failure_result,
                )
            return failure_result

        if use_source_runtime:
            # Resident services must survive their initialization window.
            # Treat an immediate exit as a failed start so System Rescue can
            # diagnose and retry it instead of publishing a false "running" state.
            for _ in range(3):
                if process.returncode is not None:
                    break
                await asyncio.sleep(0.1)
            if process.returncode is not None:
                await self._release_tool_process(request_id)
                await self.update_status(tool_id, "error")
                failure_result = {
                    "ok": False,
                    "tool_id": tool_id,
                    "request_id": request_id,
                    "error_code": "SOURCE_RUNTIME_EXITED",
                    "message": "Governed source runtime exited during startup",
                    "exit_code": process.returncode,
                }
                if not repair_attempted:
                    return await self._retry_start_after_central_repair(
                        payload,
                        tool_id,
                        tool_dir,
                        manifest,
                        failure_result,
                    )
                return failure_result

        cancel_pending = await self._register_tool_process(request_id, process)
        asyncio.create_task(
            self._watch_started_tool(
                request_id,
                tool_id,
                runtime_path,
                use_source_runtime,
                process,
                close_program_on_exit=(
                    manifest.get("has_custom_ui") is True and not background
                ),
            )
        )
        if self._background_restart_policy(tool_id) is not None:
            asyncio.create_task(
                self._reset_restart_attempts_after_stability(tool_id, process)
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
                    tool_id,
                    tool_dir,
                    manifest,
                    runtime_environment,
                )
            else:
                ui_result = {
                    "ok": False,
                    "error_code": "SOURCE_UI_UNAVAILABLE",
                    "message": "Governed source UI session is unavailable",
                }
        if ui_result and not ui_result.get("ok") and not repair_attempted:
            failure_result = {
                "tool_id": tool_id,
                "request_id": request_id,
                **ui_result,
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
                    manifest,
                    requested_mode,
                    executable_file.exists(),
                )
            ):
                fallback_payload = dict(payload)
                fallback_payload["runtime_mode"] = "executable"
                fallback_payload["_executable_fallback_attempted"] = True
                fallback_payload["_source_fallback_attempted"] = True
                return await self.start_tool(fallback_payload)
            return await self._retry_start_after_central_repair(
                payload,
                tool_id,
                tool_dir,
                manifest,
                failure_result,
            )
        if not status_result.get("ok"):
            return {
                "ok": True,
                "tool_id": tool_id,
                "request_id": request_id,
                "pid": process.pid,
                "runtime_path": str(runtime_path),
                "runtime_mode": runtime_mode,
                "status_warning": str(status_result.get("message", "status update failed")),
                "message": "Tool runtime started",
                **ui_result,
            }
        return {
            "ok": True,
            "tool_id": tool_id,
            "request_id": request_id,
            "pid": process.pid,
            "runtime_path": str(runtime_path),
            "runtime_mode": runtime_mode,
            "background": background,
            "message": "Tool runtime started",
            **ui_result,
        }

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
