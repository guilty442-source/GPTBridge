"""Tool stop, force-close, and backend shutdown."""
# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat as stat_module
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict


from .tool_lifecycle_budget import TOOL_CLOSE_BUDGET_SECONDS, deadline_after
from .toolbox_constants import _background_subprocess_kwargs, _run_hidden_subprocess
from .toolbox_shutdown_force import ForceCloseMixin


class ShutdownMixin(ForceCloseMixin):
    """Tool stop, force-close, and standalone backend shutdown."""

    async def stop_tool(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Compatibility alias for clients that still send the old stop command."""

        return await self.force_close_tool(payload)

    async def shutdown_managed_tools(self) -> Dict[str, Any]:
        """Stop main-system-owned tools while preserving independent tools.

        The process registry is the ownership boundary.  A tool is eligible
        only when this main system started an active owned process for it, and
        manifests explicitly marked ``main_system_independent_tool`` are
        excluded even if their process was registered by the toolbox.
        """
        registry = getattr(self, "_process_registry", None)
        if registry is None:
            return {"ok": True, "stopped": [], "skipped": []}
        try:
            registry.reconcile()
            records = registry.snapshot().get("processes", [])
        except Exception:
            return {"ok": False, "stopped": [], "skipped": [], "error_code": "REGISTRY_UNAVAILABLE"}

        tool_ids: set[str] = set()
        skipped: set[str] = set()
        for record in records:
            if not isinstance(record, dict):
                continue
            if not record.get("owned") or record.get("shutdown_state") in {"exited", "failed"}:
                continue
            tool_id = str(record.get("module_id") or "").strip()
            if not tool_id:
                continue
            try:
                manifest, _tool_dir = self._load_manifest_cached(tool_id)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                # Fail closed: an unresolvable tool cannot be classified as
                # main-system-owned for shutdown.
                skipped.add(tool_id)
                continue
            if manifest.get("main_system_independent_tool") is True:
                skipped.add(tool_id)
                continue
            tool_ids.add(tool_id)

        async def close_one(tool_id: str) -> tuple[str, bool]:
            try:
                # Shutdown is an internal lifecycle transition, but it still
                # passes through the normal permission sovereign.  It may
                # close infrastructure whose public manifest is unload-locked.
                self._authorize_tool_lifecycle(
                    tool_id, "stop", allow_locked=True
                )
                try:
                    from core_system.tool_isolation import get_isolation_manager

                    get_isolation_manager(self.project_root).mark_expected_stop(tool_id)
                except Exception:
                    pass
                await self._terminate_tracked_tool_processes(tool_id)
                tool_dir = self._tool_directory_for_id(tool_id)
                deadline = time.monotonic() + TOOL_CLOSE_BUDGET_SECONDS
                _, remaining = await self._run_bounded_sweep(
                    tool_id, tool_dir, deadline
                )
                if remaining:
                    return tool_id, False
                await self.update_status(tool_id, "stopped")
                return tool_id, True
            except Exception:
                return tool_id, False

        results = await asyncio.gather(*(close_one(tool_id) for tool_id in sorted(tool_ids)))
        stopped = sorted(tool_id for tool_id, ok in results if ok)
        failed = sorted(tool_id for tool_id, ok in results if not ok)
        return {
            "ok": not failed,
            "stopped": stopped,
            "failed": failed,
            "skipped": sorted(skipped),
        }

    def _sweep_and_stop_source_tool_processes(
        self,
        tool_id: str,
        tool_dir: Path,
        source_entry: Path,
        executable_file: Path,
    ) -> tuple[set[int], list[int]]:
        # One native process pass (psutil, milliseconds): discovery and each
        # stop share the same snapshot.  The old two-attempt PowerShell sweep
        # cost up to sixteen 1-3s CIM calls per close.  Runs via
        # asyncio.to_thread so it never stalls the backend event loop.
        def probe() -> list[int]:
            return sorted(
                set(self._running_source_runtime_process_ids(source_entry))
                | set(self._running_executable_process_ids(executable_file))
                | set(self._running_packaged_backend_process_ids(tool_dir))
                | set(self._running_source_ui_process_ids(tool_id))
            )

        def stop() -> set[int]:
            out: set[int] = set()
            out.update(self._stop_running_source_runtime(source_entry))
            out.update(self._stop_running_executable(executable_file))
            out.update(self._stop_running_packaged_backend(tool_dir))
            out.update(self._stop_running_source_ui(tool_id))
            return out

        return self._settled_sweep(stop, probe)

    def _sweep_and_stop_tool_processes(
        self,
        tool_id: str,
        tool_dir: Path,
        executable_file: Path,
    ) -> tuple[set[int], list[int]]:
        def probe() -> list[int]:
            return sorted(
                set(self._running_executable_process_ids(executable_file))
                | set(self._running_packaged_backend_process_ids(tool_dir))
                | set(self._running_source_ui_process_ids(tool_id))
            )

        def stop() -> set[int]:
            out: set[int] = set()
            out.update(self._stop_running_executable(executable_file))
            out.update(self._stop_running_packaged_backend(tool_dir))
            out.update(self._stop_running_source_ui(tool_id))
            return out

        return self._settled_sweep(stop, probe)

    @staticmethod
    def _settled_sweep(
        stop: Any, probe: Any
    ) -> tuple[set[int], list[int]]:
        # TerminateProcess/psutil kill only signals teardown — the OS keeps
        # a dying process enumerable for a short window, so an immediate
        # re-probe reports "remains after forced close" for a process that
        # is already dead.  Re-kill and re-probe until the list empties or
        # the settle window closes; a respawned pid is killed on the next
        # pass.  The window stays well under the A540 five-second close
        # budget enforced by the caller's wait_for.
        stopped: set[int] = set()
        settle_deadline = time.monotonic() + 1.0
        while True:
            stopped.update(stop())
            remaining = probe()
            if not remaining or time.monotonic() >= settle_deadline:
                return stopped, remaining
            time.sleep(0.1)

    @staticmethod
    def _require_unlinked_descriptor_paths(candidates: tuple[Path, ...]) -> None:
        """Fail closed when any descriptor path is a link or reparse point."""
        for candidate in candidates:
            metadata = candidate.lstat()
            attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
            if stat_module.S_ISLNK(metadata.st_mode) or bool(attributes & 0x400):
                raise ValueError("standalone backend descriptor path is linked")

    def _standalone_backend_descriptor(
        self, tool_id: str, standalone_root: Path
    ) -> tuple[int, int, str] | None:
        """Return the validated (pid, port, shutdown token) or None.

        None means no trustworthy backend descriptor exists, so the caller
        reports ``backend_running: False`` instead of guessing.
        """
        ipc_root = standalone_root / "runtime" / "ipc"
        owner_path = ipc_root / f"standalone-{tool_id}-backend.json"
        token_path = ipc_root / "session-token"
        try:
            self._require_unlinked_descriptor_paths(
                (standalone_root, ipc_root, owner_path, token_path)
            )
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            pid = int(owner.get("pid") or 0)
            port = int(owner.get("backend_port") or 0)
            shutdown_token = str(owner.get("shutdown_token") or "").strip()
            expected_root = self.project_root.resolve()
            expected_instance = hashlib.sha256(
                os.path.normcase(str(expected_root)).replace("\\", "/").encode("utf-8")
            ).hexdigest()[:24]
            valid = (
                str(owner.get("tool_id") or "") == tool_id
                and Path(str(owner.get("project_root") or "")).resolve()
                == expected_root
                and str(owner.get("workspace_instance_id") or "") == expected_instance
                and pid > 0
                and 1024 <= port <= 65535
                and re.fullmatch(r"[a-f0-9]{64}", shutdown_token) is not None
            )
            if not valid:
                return None
            return pid, port, shutdown_token
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _request_backend_shutdown(port: int, shutdown_token: str, reason: str) -> None:
        """Best-effort graceful shutdown request (bounded HTTP timeout)."""
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
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=2):
                pass
        except (OSError, urllib.error.URLError):
            pass

    @staticmethod
    def _standalone_backend_main(
        standalone_root: Path, tool_id: str
    ) -> Path:
        return (
            standalone_root
            / "dist"
            / "resources"
            / "app"
            / "independent_tool"
            / tool_id
            / "src"
            / "channel_runtime.py"
        )

    async def _await_standalone_backend_exit(
        self, pid: int, expected_main: Path, grace_seconds: float = 3.0
    ) -> bool:
        """Wait inside the grace window; True when the backend exited."""
        shutdown_deadline = deadline_after(grace_seconds)
        while time.monotonic() < shutdown_deadline:
            if not self._validated_standalone_backend_pid(pid, expected_main):
                return True
            await asyncio.sleep(0.15)
        return False

    async def _force_kill_standalone_backend(
        self, pid: int, expected_main: Path
    ) -> None:
        if os.name != "nt" or not self._validated_standalone_backend_pid(
            pid, expected_main
        ):
            return
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
        descriptor = self._standalone_backend_descriptor(tool_id, standalone_root)
        if descriptor is None:
            return {"ok": True, "tool_id": tool_id, "backend_running": False}
        pid, port, shutdown_token = descriptor
        await asyncio.to_thread(
            self._request_backend_shutdown, port, shutdown_token, reason
        )
        expected_main = self._standalone_backend_main(standalone_root, tool_id)
        if await self._await_standalone_backend_exit(pid, expected_main):
            return {"ok": True, "tool_id": tool_id, "backend_stopped": True}
        await self._force_kill_standalone_backend(pid, expected_main)
        if self._validated_standalone_backend_pid(pid, expected_main):
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
