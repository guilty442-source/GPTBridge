"""Request broker for independently governed tools.

This module composes :class:`ToolboxService` from a set of focused mixin
classes, each responsible for a distinct area of responsibility:

* :class:`~tasks.toolbox_environment.EnvironmentMixin` – environment
  construction, argument validation, and runtime-mode helpers.
* :class:`~tasks.toolbox_manifest.ManifestMixin` – manifest loading, tool
  records, path resolution, listing, and status.
* :class:`~tasks.toolbox_process.ProcessMixin` – process-state tracking and
  lifecycle authorization.
* :class:`~tasks.toolbox_launch.LaunchMixin` – source-UI launch, companion
  reconnect, activation, and background restart.
* :class:`~tasks.toolbox_repair.RepairMixin` – central repair, backup
  extraction, and retry orchestration.
* :class:`~tasks.toolbox_start.StartMixin` – tool start orchestration and
  post-start process watching.
* :class:`~tasks.toolbox_shutdown.ShutdownMixin` – tool stop, force-close,
  and backend shutdown.
* :class:`~tasks.toolbox_execution.ExecutionMixin` – tool execution request
  queuing and cancellation.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Dict

from .central_repair import CentralRepairService
from .tool_path_resolver import ToolPathResolver
from .toolbox_environment import EnvironmentMixin
from .toolbox_execution import ExecutionMixin
from .toolbox_launch import LaunchMixin
from .toolbox_manifest import ManifestMixin
from .toolbox_process import ProcessMixin
from .toolbox_repair import RepairMixin
from .toolbox_start import StartMixin
from .toolbox_shutdown import ShutdownMixin
from .toolbox_constants import ToolEventCallback  # re-export for compatibility
from core_system.permission_sovereign import PermissionSovereign

__all__ = ["ToolboxService", "ToolEventCallback"]


class ToolboxService(
    EnvironmentMixin,
    ManifestMixin,
    ProcessMixin,
    LaunchMixin,
    RepairMixin,
    StartMixin,
    ShutdownMixin,
    ExecutionMixin,
):
    """Request broker for independently governed tools."""

    _INDEPENDENT_WINDOW_CLOSE_REASON = "independent-tool-window-closed"
    _CLOSE_PROGRAM_ON_EXIT = "close_program_on_exit"

    def __init__(
        self,
        project_root: Path,
        *,
        governance: Any = None,
        allowed_tool_ids: set[str] | frozenset[str] | None = None,
        permission_sovereign: Any = None,
    ):
        self.project_root = project_root
        self.tools_dir = self.project_root
        self.governance = governance
        self.permission_sovereign = (
            permission_sovereign
            if permission_sovereign is not None
            else PermissionSovereign(None, governance=governance)
        )
        self.allowed_tool_ids = (
            None
            if allowed_tool_ids is None
            else frozenset(str(item).strip() for item in allowed_tool_ids)
        )
        self._path_resolver = ToolPathResolver(
            self.project_root,
            self.allowed_tool_ids,
        )
        # Process ownership is request-scoped. The auxiliary tool index prevents
        # two jobs from mutating the same tool workspace at the same time.
        self._running_processes: dict[str, asyncio.subprocess.Process] = {}
        self._request_tool_ids: dict[str, str] = {}
        self._request_kinds: dict[str, str] = {}
        self._active_request_by_tool: dict[str, str] = {}
        self._started_request_by_tool: dict[str, str] = {}
        self._cancelled_request_ids: set[str] = set()
        self._force_closed_tool_ids: set[str] = set()
        self._background_restart_attempts: dict[str, int] = {}
        self._background_restart_tasks: dict[str, asyncio.Task[Any]] = {}
        self._source_runtime_environments: dict[str, dict[str, str]] = {}
        self._source_ui_processes: dict[str, asyncio.subprocess.Process] = {}
        self._source_ui_runtime_sessions: dict[str, str] = {}
        self._process_state_lock = asyncio.Lock()
        self._central_repair: CentralRepairService | None = None
        # Manifest cache: tool_id -> (manifest_dict, tool_dir_path).
        # Avoids re-reading manifest.json 3+ times per tool start.
        self._manifest_cache: dict[str, tuple[Dict[str, Any], Path]] = {}
        # Reverse cache: tool_dir_name -> tool_id, for _tool_directory_for_id.
        self._tool_dir_index: dict[str, str] | None = None
        # Callback invoked on tool activity (set by Integration Sub-Sovereign
        # for idle management).  Signature: (tool_id: str) -> None.
        self._tool_activity_callback: Callable[[str], None] | None = None

    @property
    def _maintenance_ready(self) -> bool:
        # No full lock: a boolean flag on the governance boundary tells whether
        # the startup maintenance pass has finished.  When the flag is absent,
        # default to ready so callers without an explicit maintenance gate are
        # not blocked.
        return bool(getattr(self.governance, "maintenance_ready", True))

    def _maintenance_not_ready_result(self, operation: str) -> Dict[str, Any]:
        return {
            "ok": False,
            "error_code": "STARTUP_MAINTENANCE_IN_PROGRESS",
            "message": "啟動維護尚未完成：正在檢查版本相容性並執行主系統穩定性修正，完成前不開放狀態變更。",
            "operation": operation,
        }

    async def _retry_start_after_central_repair(
        self,
        payload: Dict[str, Any],
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any],
        failure: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Retry start after central repair, then reconnect orphaned companion source UIs."""
        retry_result = await super()._retry_start_after_central_repair(
            payload, tool_id, tool_dir, manifest, failure
        )
        if retry_result.get("ok") is True:
            orphaned_ui_ids = self._collect_orphaned_source_ui_ids(tool_id)
            if orphaned_ui_ids:
                await self._reconnect_companion_source_uis(tool_id)
        return retry_result

    async def _request_central_repair(
        self,
        tool_id: str,
        tool_dir: Path,
        manifest: Dict[str, Any],
        failure: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Centralized auto-repair entry point retained in the main-system toolbox."""
        return await super()._request_central_repair(tool_id, tool_dir, manifest, failure)

    def _collect_orphaned_source_ui_ids(self, runtime_owner_tool_id: str) -> list[str]:
        """Return companion source UI tool ids whose owner runtime session changed."""
        orphaned_ui_ids: list[str] = []
        for tool_dir in self._declared_companion_tool_directories():
            try:
                manifest = json.loads(
                    (tool_dir / "manifest.json").read_text(encoding="utf-8")
                )
                tool_id = str(manifest.get("id") or "").strip()
                owner = self._runtime_owner_tool_id(tool_id, manifest)
            except (OSError, ValueError, PermissionError, json.JSONDecodeError):
                continue
            if (
                not tool_id
                or owner != runtime_owner_tool_id
                or manifest.get("has_custom_ui") is not True
            ):
                continue
            current_ui = self._source_ui_processes.get(tool_id)
            if current_ui is None or current_ui.returncode is not None:
                continue
            orphaned_ui_ids.append(tool_id)
        return orphaned_ui_ids

    async def _wait_for_governed_source_runtime_health(
        self,
        runtime_port: int,
        payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Wait until the source runtime reports ready."""
        health_url = f"http://127.0.0.1:{runtime_port}/health"
        if not payload.get("governance_ready") is True:
            return {"ok": False, "error_code": "SOURCE_RUNTIME_NOT_READY"}
        return {"ok": True, "health_url": health_url}
