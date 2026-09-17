"""Source-UI launch, companion reconnect, and activation.

A184/E159: The main system may launch tools and follow their declared
window-close policy, but it must NOT act as tool supervisor or watchdog.
Auto-restart of crashed tools is the tool owner's own responsibility.
"""
# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict

from managers.process_utils import terminate_process_tree

from .tool_lifecycle_budget import (
    TOOL_OPEN_BUDGET_SECONDS,
    budget_evidence,
)
from .toolbox_constants import _background_subprocess_kwargs
from core_system.versioning import component_version
from tool_codenames import get_tool_codename
from .toolbox_launch_helpers import (
    _check_source_runtime_ready,
    _resolve_source_ui_paths,
    _build_source_ui_environment,
)

_CENTRAL_VERSION = component_version("toolbox")
_CENTRAL_CODENAME = get_tool_codename("main-system")


def _source_runtime_port(runtime_environment: dict[str, str]) -> int:
    """Published IPC port for the governed source runtime (0 when absent)."""
    try:
        return int(runtime_environment["GPTBRIDGE_IPC_PORT"])
    except (KeyError, TypeError, ValueError):
        return 0


class LaunchMixin:
    """Source-UI launch, companion reconnect, and activation."""

    # ------------------------------------------------------------------
    # Source-UI launch
    # ------------------------------------------------------------------

    async def _await_source_runtime_ready(
        self,
        runtime_port: int,
        expected_runtime_tool_id: str,
        deadline: float,
    ) -> bool:
        """Poll governed runtime health until ready or the open budget ends.

        Local governed runtimes normally publish health in well under a second;
        the wait stays inside the five-second open budget so a cold or
        unhealthy runtime can never block the window for longer.
        """
        while time.monotonic() < deadline:
            if await asyncio.to_thread(
                _check_source_runtime_ready,
                runtime_port,
                expected_runtime_tool_id,
                self._workspace_instance_id(),
            ):
                return True
            await asyncio.sleep(0.02)
        return False

    def _source_ui_session_fingerprint(
        self,
        runtime_port: int,
        runtime_environment: dict[str, str],
        expected_runtime_tool_id: str,
    ) -> str:
        return hashlib.sha256(
            "\0".join(
                (
                    expected_runtime_tool_id,
                    str(runtime_port),
                    str(runtime_environment.get("GPTBRIDGE_IPC_SESSION_TOKEN") or ""),
                    self._workspace_instance_id(),
                )
            ).encode("utf-8")
        ).hexdigest()

    async def _reuse_or_retire_source_ui(
        self,
        tool_id: str,
        session_fingerprint: str,
    ) -> dict[str, Any] | None:
        """Return the reuse result, or retire a stale or orphaned UI host."""
        existing = self._source_ui_processes.get(tool_id)
        if existing is None:
            # A UI inherited from an earlier main-system generation cannot
            # carry the newly issued runtime token or port. Remove the stale
            # process before creating the replacement session.
            orphaned_ui_ids = await asyncio.to_thread(
                self._running_source_ui_process_ids,
                tool_id,
            )
            if orphaned_ui_ids:
                await asyncio.to_thread(self._stop_running_source_ui, tool_id)
            return None
        if existing.returncode is None:
            if self._source_ui_runtime_sessions.get(tool_id) == session_fingerprint:
                return {"ui_pid": existing.pid}
            self._source_ui_processes.pop(tool_id, None)
            self._source_ui_runtime_sessions.pop(tool_id, None)
            await terminate_process_tree(existing)
            try:
                await asyncio.wait_for(existing.wait(), timeout=1)
            except asyncio.TimeoutError:
                await terminate_process_tree(existing)
        return None

    async def _spawn_source_ui_host(
        self,
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any],
        runtime_environment: dict[str, str],
        expected_runtime_tool_id: str,
        ui_paths: dict[str, Path],
    ) -> Any:
        environment = self._tool_environment(
            tool_id, tool_dir, manifest,
            governance_tool_id=expected_runtime_tool_id,
        )
        environment = _build_source_ui_environment(
            environment, tool_id, tool_dir, manifest,
            expected_runtime_tool_id, runtime_environment,
            ui_paths["renderer_entry"], self.project_root,
            self._workspace_instance_id(),
        )
        return await asyncio.create_subprocess_exec(
            str(ui_paths["electron"]),
            str(ui_paths["host_entry"]),
            f"--tool-id={tool_id}",
            cwd=str(self.project_root / "main-system"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=environment,
            **_background_subprocess_kwargs(),
        )

    async def _forget_source_ui_on_exit(self, tool_id: str, process: Any) -> None:
        """Release the UI session when its window closes; close the tool."""
        await process.wait()
        if self._source_ui_processes.get(tool_id) is not process:
            return
        self._source_ui_processes.pop(tool_id, None)
        self._source_ui_runtime_sessions.pop(tool_id, None)
        if tool_id in self._force_closed_tool_ids:
            return
        try:
            await self.force_close_tool(
                {
                    "tool_id": tool_id,
                    "request_id": f"window-close-{uuid.uuid4().hex}",
                    "reason": "independent-tool-window-closed",
                }
            )
        except Exception:
            await self.update_status(tool_id, "stopped")

    def _checked_source_ui_paths(
        self, tool_id: str, started: float
    ) -> tuple[dict[str, Path] | None, dict[str, Any] | None]:
        """Resolve the host/renderer/electron files or an error verdict."""
        ui_paths = _resolve_source_ui_paths(self.project_root, tool_id)
        if all(path.is_file() for path in ui_paths.values()):
            return ui_paths, None
        return None, {
            "ok": False,
            "error_code": "SOURCE_UI_UNAVAILABLE",
            "message": "Governed source UI host or renderer is unavailable",
            **budget_evidence(started, TOOL_OPEN_BUDGET_SECONDS),
        }

    def _source_ui_started_result(
        self, process: Any, started: float
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "ui_pid": process.pid,
            "ui_mode": "governed-source-ui",
            **budget_evidence(started, TOOL_OPEN_BUDGET_SECONDS),
        }

    async def _launch_source_ui(
        self,
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any],
        runtime_environment: dict[str, str],
        *,
        runtime_tool_id: str | None = None,
    ) -> dict[str, Any]:
        started = time.monotonic()
        deadline = started + TOOL_OPEN_BUDGET_SECONDS
        expected_runtime_tool_id = runtime_tool_id or tool_id
        runtime_port = _source_runtime_port(runtime_environment)
        if not await self._await_source_runtime_ready(
            runtime_port, expected_runtime_tool_id, deadline
        ):
            return {
                "ok": False,
                "error_code": "SOURCE_RUNTIME_NOT_READY",
                "message": "Governed source runtime did not become ready",
                **budget_evidence(started, TOOL_OPEN_BUDGET_SECONDS),
            }
        session_fingerprint = self._source_ui_session_fingerprint(
            runtime_port, runtime_environment, expected_runtime_tool_id
        )
        reusable = await self._reuse_or_retire_source_ui(
            tool_id, session_fingerprint
        )
        if reusable is not None:
            return {
                "ok": True,
                "ui_mode": "governed-source-ui",
                **reusable,
                **budget_evidence(started, TOOL_OPEN_BUDGET_SECONDS),
            }
        ui_paths, paths_error = self._checked_source_ui_paths(tool_id, started)
        if paths_error is not None:
            return paths_error
        process = await self._spawn_source_ui_host(
            tool_id,
            tool_dir,
            manifest,
            runtime_environment,
            expected_runtime_tool_id,
            ui_paths,
        )
        self._source_ui_processes[tool_id] = process
        self._source_ui_runtime_sessions[tool_id] = session_fingerprint
        asyncio.create_task(self._forget_source_ui_on_exit(tool_id, process))
        return self._source_ui_started_result(process, started)

    # ------------------------------------------------------------------
    # Companion reconnect
    # ------------------------------------------------------------------

    def _owner_runtime_ui_tools(
        self, runtime_owner_tool_id: str
    ) -> list[tuple[str, Path, Dict[str, Any]]]:
        """UI tools whose runtime is owned by the given runtime identity."""
        selected: list[tuple[str, Path, Dict[str, Any]]] = []
        seen: set[str] = set()
        for record in self._load_manifest_records():
            tool_id = str(record.get("id") or "").strip()
            if (
                not tool_id
                or tool_id in seen
                or tool_id == runtime_owner_tool_id
                or record.get("has_custom_ui") is not True
            ):
                continue
            seen.add(tool_id)
            try:
                manifest, tool_dir = self._load_manifest_cached(tool_id)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            try:
                owner = self._runtime_owner_tool_id(tool_id, manifest)
            except PermissionError:
                continue
            if owner == runtime_owner_tool_id:
                selected.append((tool_id, tool_dir, manifest))
        return selected

    async def _reconnect_companion_source_uis(self, runtime_owner_tool_id: str) -> None:
        """Reconnect open tool windows after their owner runtime changes session."""

        runtime_environment = self._source_runtime_environments.get(runtime_owner_tool_id)
        if runtime_environment is None:
            return
        for tool_id, tool_dir, manifest in self._owner_runtime_ui_tools(
            runtime_owner_tool_id
        ):
            current_ui = self._source_ui_processes.get(tool_id)
            if current_ui is None or current_ui.returncode is not None:
                continue
            await self._launch_source_ui(
                tool_id,
                tool_dir,
                manifest,
                runtime_environment,
                runtime_tool_id=runtime_owner_tool_id,
            )

    # ------------------------------------------------------------------
    # Existing-tool-window activation
    # ------------------------------------------------------------------

    async def _activate_existing_tool_window(
        self,
        *,
        tool_id: str,
        tool_dir: Path,
        executable_file: Path,
        manifest: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            activation_process = await asyncio.create_subprocess_exec(
                str(executable_file),
                cwd=str(tool_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self._tool_environment(
                    tool_id,
                    tool_dir,
                    manifest,
                ),
                **_background_subprocess_kwargs(),
            )
        except Exception as exc:
            return {
                "activated": False,
                "activation_error": str(exc),
            }

        async def reap_activation_process() -> None:
            try:
                await activation_process.wait()
            except Exception:
                return

        asyncio.create_task(reap_activation_process())
        return {
            "activated": True,
            "activation_pid": activation_process.pid,
        }
