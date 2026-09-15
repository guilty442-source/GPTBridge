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
from .toolbox_shutdown_force import ForceCloseMixin


class ShutdownMixin(ForceCloseMixin):
    """Tool stop, force-close, and standalone backend shutdown."""

    async def stop_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Compatibility alias for clients that still send the old stop command."""

        return await self.force_close_tool(payload)

    def _sweep_and_stop_source_tool_processes(
        self,
        tool_id: str,
        tool_dir: Path,
        source_entry: Path,
        executable_file: Path,
    ) -> tuple[set[int], list[int]]:
        # Synchronous PowerShell process sweeps; run via asyncio.to_thread so
        # a slow sweep cannot stall the main backend event loop.
        stopped: set[int] = set()
        for _attempt in range(2):
            stopped.update(self._stop_running_source_runtime(source_entry))
            stopped.update(self._stop_running_executable(executable_file))
            stopped.update(self._stop_running_packaged_backend(tool_dir))
            stopped.update(self._stop_running_source_ui(tool_id))
        remaining = sorted(
            set(self._running_source_runtime_process_ids(source_entry))
            | set(self._running_executable_process_ids(executable_file))
            | set(self._running_packaged_backend_process_ids(tool_dir))
            | set(self._running_source_ui_process_ids(tool_id))
        )
        return stopped, remaining

    def _sweep_and_stop_tool_processes(
        self,
        tool_id: str,
        tool_dir: Path,
        executable_file: Path,
    ) -> tuple[set[int], list[int]]:
        stopped: set[int] = set()
        for _attempt in range(2):
            stopped.update(self._stop_running_executable(executable_file))
            stopped.update(self._stop_running_packaged_backend(tool_dir))
            stopped.update(self._stop_running_source_ui(tool_id))
        remaining = sorted(
            set(self._running_executable_process_ids(executable_file))
            | set(self._running_packaged_backend_process_ids(tool_dir))
            | set(self._running_source_ui_process_ids(tool_id))
        )
        return stopped, remaining

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
                _opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({})
                )
                with _opener.open(request, timeout=5):
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
