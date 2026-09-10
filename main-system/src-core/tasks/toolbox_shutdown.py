"""Tool stop, force-close, and backend shutdown."""
# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat as stat_module
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

from managers.process_utils import terminate_process_tree

from .toolbox_constants import _background_subprocess_kwargs, _run_hidden_subprocess


class ShutdownMixin:
    """Tool stop, force-close, and standalone backend shutdown."""

    async def stop_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Compatibility alias for clients that still send the old stop command."""

        return await self.force_close_tool(payload)

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
                    for _attempt in range(2):
                        force_closed_process_ids.update(
                            self._stop_running_source_runtime(source_entry)
                        )
                        force_closed_process_ids.update(
                            self._stop_running_executable(executable_file)
                        )
                        force_closed_process_ids.update(
                            self._stop_running_packaged_backend(tool_dir)
                        )
                        force_closed_process_ids.update(
                            self._stop_running_source_ui(tool_id)
                        )
                    remaining_process_ids = sorted(
                        set(self._running_source_runtime_process_ids(source_entry))
                        | set(self._running_executable_process_ids(executable_file))
                        | set(self._running_packaged_backend_process_ids(tool_dir))
                        | set(self._running_source_ui_process_ids(tool_id))
                    )
                else:
                    for _attempt in range(2):
                        force_closed_process_ids.update(
                            self._stop_running_executable(executable_file)
                        )
                        force_closed_process_ids.update(
                            self._stop_running_packaged_backend(tool_dir)
                        )
                        force_closed_process_ids.update(
                            self._stop_running_source_ui(tool_id)
                        )
                    remaining_process_ids = sorted(
                        set(self._running_executable_process_ids(executable_file))
                        | set(self._running_packaged_backend_process_ids(tool_dir))
                        | set(self._running_source_ui_process_ids(tool_id))
                    )
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

    async def shutdown_tool_backend(
        self,
        tool_id: str,
        *,
        reason: str = "hot-update",
    ) -> Dict[str, Any]:
        """Stop a validated standalone backend so updated Python is reloaded."""

        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", tool_id):
            return {"ok": False, "error_code": "INVALID_TOOL_ID"}
        try:
            self._authorize_tool_lifecycle(tool_id, "stop")
        except PermissionError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "PERMISSION_DENIED",
                "message": "PERMISSION_DENIED",
            }
        try:
            standalone_root = self._tool_directory_for_id(tool_id)
        except ValueError:
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "TOOL_NOT_FOUND",
                "message": "Tool directory is unavailable",
            }
        ipc_root = standalone_root / "runtime" / "ipc"
        owner_path = ipc_root / f"standalone-{tool_id}-backend.json"
        token_path = ipc_root / "session-token"
        try:
            for candidate in (standalone_root, ipc_root, owner_path, token_path):
                metadata = candidate.lstat()
                attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
                if stat_module.S_ISLNK(metadata.st_mode) or bool(attributes & 0x400):
                    raise ValueError("standalone backend descriptor path is linked")
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            pid = int(owner.get("pid") or 0)
            port = int(owner.get("backend_port") or 0)
            shutdown_token = str(owner.get("shutdown_token") or "").strip()
            expected_root = self.project_root.resolve()
            expected_instance = hashlib.sha256(
                os.path.normcase(str(expected_root)).replace("\\", "/").encode("utf-8")
            ).hexdigest()[:24]
            if (
                str(owner.get("tool_id") or "") != tool_id
                or Path(str(owner.get("project_root") or "")).resolve()
                != expected_root
                or str(owner.get("workspace_instance_id") or "")
                != expected_instance
                or pid <= 0
                or not 1024 <= port <= 65535
                or not re.fullmatch(r"[a-f0-9]{64}", shutdown_token)
            ):
                raise ValueError("standalone backend descriptor is invalid")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return {"ok": True, "tool_id": tool_id, "backend_running": False}

        def request_shutdown() -> None:
            request = urllib.request.Request(
                f"http://127.0.0.1:{port}/shutdown",
                data=b"",
                headers={
                    "X-GPTBridge-Shutdown-Token": shutdown_token,
                    "X-GPTBridge-Shutdown-Reason": reason,
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=5):
                    pass
            except (OSError, urllib.error.URLError):
                pass

        await asyncio.to_thread(request_shutdown)
        expected_main = (
            standalone_root
            / "dist"
            / "resources"
            / "app"
            / "independent_tool"
            / tool_id
            / "src"
            / "channel_runtime.py"
        )
        for _ in range(20):
            if not self._validated_standalone_backend_pid(
                pid,
                expected_main,
            ):
                return {"ok": True, "tool_id": tool_id, "backend_stopped": True}
            await asyncio.sleep(0.25)

        if os.name == "nt" and self._validated_standalone_backend_pid(
            pid,
            expected_main,
        ):
            killer = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **_background_subprocess_kwargs(),
            )
            await killer.communicate()
        if self._validated_standalone_backend_pid(
            pid,
            expected_main,
        ):
            return {
                "ok": False,
                "tool_id": tool_id,
                "error_code": "BACKEND_SHUTDOWN_FAILED",
            }
        return {"ok": True, "tool_id": tool_id, "backend_stopped": True}

    @staticmethod
    def _validated_standalone_backend_pid(pid: int, expected_main: Path) -> bool:
        if os.name != "nt" or pid <= 0:
            return False
        env = os.environ.copy()
        env["GPTBRIDGE_BACKEND_PID"] = str(pid)
        env["GPTBRIDGE_EXPECTED_MAIN"] = str(expected_main.resolve(strict=False))
        command = (
            "$pidValue=[int]$env:GPTBRIDGE_BACKEND_PID;"
            "$expected=[IO.Path]::GetFullPath($env:GPTBRIDGE_EXPECTED_MAIN);"
            "$p=Get-CimInstance Win32_Process -Filter \"ProcessId=$pidValue\" "
            "-ErrorAction SilentlyContinue;"
            "if($p -and $p.CommandLine -and "
            "$p.CommandLine.IndexOf($expected,[StringComparison]::OrdinalIgnoreCase) -ge 0){'1'}"
        )
        try:
            completed = _run_hidden_subprocess(
                [
                    "powershell",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    command,
                ],
                env=env,
            )
        except Exception:
            return False
        return completed.returncode == 0 and completed.stdout.strip() == "1"
