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
  reconnect, and activation.
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
import logging
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict

from .central_repair import CentralRepairService
from .tool_path_resolver import ToolPathResolver


_logger = logging.getLogger("gptbridge.toolbox_service")
from .toolbox_environment import EnvironmentMixin
from .toolbox_execution import ExecutionMixin
from .toolbox_launch import LaunchMixin
from .toolbox_manifest import ManifestMixin
from .toolbox_process import ProcessMixin
from .toolbox_repair import RepairMixin
from .toolbox_start import StartMixin
from .toolbox_shutdown import ShutdownMixin
from .toolbox_constants import ToolEventCallback  # re-export for compatibility
from core_system.process_registry import get_process_registry
from governance import PermissionSovereign

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
        self.tools_dir = self.project_root / "Standalone tools"
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
        self._source_runtime_environments: dict[str, dict[str, str]] = {}
        self._source_ui_processes: dict[str, asyncio.subprocess.Process] = {}
        self._source_ui_runtime_sessions: dict[str, str] = {}
        self._process_state_lock = asyncio.Lock()
        # Per-tool start serialization: prevents 15 concurrent resident
        # starts (G83) when multiple callers race on shared-layer.
        self._tool_start_locks: dict[str, asyncio.Lock] = {}
        self._tool_start_lock_guard = asyncio.Lock()
        self._registry_reconcile_task: asyncio.Task[Any] | None = None
        self._registry_reconcile_stop = asyncio.Event()
        self._registry_reconcile_core = False
        self._process_registry = get_process_registry(
            self.project_root / "main-system" / "runtime" / "state"
            / "process-registry.json"
        )
        # G47: unified module-level dual-axis runtime state (star-runtime-state/v1)
        from core_system.runtime_state_registry import RuntimeStateRegistry

        self._runtime_state_registry = RuntimeStateRegistry(
            self.project_root / "main-system" / "runtime" / "state"
            / "runtime-state-registry.json",
            project_root=self.project_root,
        )
        # G83-2: immediate sweep of leftovers from previous crash-loop session
        try:
            self._process_registry.reconcile()
        except Exception:
            pass
        self._central_repair: CentralRepairService | None = None
        # Manifest cache: tool_id -> (manifest_dict, tool_dir_path).
        # Avoids re-reading manifest.json 3+ times per tool start.  The
        # mtime/size keys keep the cache fresh when a manifest is edited
        # while the backend keeps running.
        self._manifest_cache: dict[str, tuple[Dict[str, Any], Path]] = {}
        self._manifest_cache_keys: dict[str, tuple[int, int]] = {}
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

    def reconcile_process_registry(self) -> dict[str, int]:
        """Reconcile owned tool PIDs through the canonical registry."""
        return self._process_registry.reconcile()

    async def _registry_reconcile_once(self) -> None:
        """One reconcile pass — governed-flow tick and private-loop body."""
        if self._registry_reconcile_stop.is_set():
            return
        try:
            await asyncio.to_thread(self.reconcile_process_registry)
        except Exception:
            pass

    async def start_process_registry_monitor(
        self,
        interval_seconds: float = 30.0,
        automation_core: Any | None = None,
    ) -> None:
        """Periodically reconcile owned PIDs while the main system is alive.

        §1.1 自動化集中：automation core 為唯一註冊點；kill-switch 拒絕
        時不回落私有迴圈（否則 kill switch 可繞過）。無核心時才保留
        私有 fallback。
        """
        if (
            self._registry_reconcile_task is not None
            or self._registry_reconcile_core
        ):
            return
        interval = max(1.0, float(interval_seconds))
        self._registry_reconcile_stop.clear()

        if automation_core is not None:
            self._registry_reconcile_core = bool(
                automation_core.register_flow(
                    "toolbox-registry-reconcile",
                    self._registry_reconcile_once,
                    interval_s=interval,
                    pausable=True,
                )
            )
            return

        async def loop() -> None:
            # P7: per-tick deadline — a hung reconcile probe must not
            # freeze the private loop (the automation-core path is
            # bounded by the scheduler's wait_for tick wrapper).
            tick_deadline = max(30.0, min(600.0, interval * 5))
            while not self._registry_reconcile_stop.is_set():
                try:
                    await asyncio.wait_for(
                        self._registry_reconcile_once(), timeout=tick_deadline
                    )
                except asyncio.TimeoutError:
                    _logger.warning(
                        "registry reconcile exceeded %.0fs deadline",
                        tick_deadline,
                    )
                try:
                    await asyncio.wait_for(
                        self._registry_reconcile_stop.wait(), timeout=interval
                    )
                except asyncio.TimeoutError:
                    continue

        self._registry_reconcile_task = asyncio.create_task(
            loop(), name="toolbox-process-registry-reconcile"
        )

    async def stop_process_registry_monitor(
        self, automation_core: Any | None = None
    ) -> None:
        """Stop the registry reconciler before managed tool shutdown."""
        self._registry_reconcile_stop.set()
        if self._registry_reconcile_core and automation_core is not None:
            automation_core.unregister("toolbox-registry-reconcile")
        self._registry_reconcile_core = False
        task = self._registry_reconcile_task
        self._registry_reconcile_task = None
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def _acquire_cross_process_start_lock(self, tool_id: str) -> bool:
        """Atomically reserve a tool start across backend processes.

        The per-process asyncio lock prevents local races; this directory
        reservation closes the cross-process gap.  Dead owners are reclaimed
        only after the canonical PID liveness check, otherwise startup fails
        closed instead of creating a duplicate resident process.
        """
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", tool_id) is None:
            return False
        lock_root = (
            self.project_root / "main-system" / "runtime" / "state"
            / "tool-start-locks"
        )
        lock_path = lock_root / tool_id
        try:
            lock_path.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            owner_path = lock_path / "owner.json"
            try:
                owner = json.loads(owner_path.read_text(encoding="utf-8"))
                owner_pid = int(owner.get("pid") or 0)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                owner_pid = 0
            if owner_pid > 0 and self._process_registry._pid_alive(owner_pid):
                return False
            try:
                owner_path.unlink(missing_ok=True)
                lock_path.rmdir()
            except OSError:
                return False
            try:
                lock_path.mkdir(parents=False, exist_ok=False)
            except FileExistsError:
                return False
        except OSError:
            return False

        try:
            (lock_path / "owner.json").write_text(
                json.dumps(
                    {"pid": os.getpid(), "tool_id": tool_id, "acquired_at": time.time()},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            return True
        except OSError:
            try:
                (lock_path / "owner.json").unlink(missing_ok=True)
                lock_path.rmdir()
            except OSError:
                pass
            return False

    def _release_cross_process_start_lock(self, tool_id: str) -> None:
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", tool_id) is None:
            return
        lock_path = (
            self.project_root / "main-system" / "runtime" / "state"
            / "tool-start-locks" / tool_id
        )
        try:
            owner_path = lock_path / "owner.json"
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            if int(owner.get("pid") or 0) != os.getpid():
                return
            owner_path.unlink(missing_ok=True)
            lock_path.rmdir()
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass

    async def _get_tool_start_lock(self, tool_id: str) -> asyncio.Lock:
        """Per-tool lock for serializing concurrent starts (G83 dedup)."""
        async with self._tool_start_lock_guard:
            lock = self._tool_start_locks.get(tool_id)
            if lock is None:
                lock = asyncio.Lock()
                self._tool_start_locks[tool_id] = lock
            return lock

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
        """Return open UI tool ids whose owner runtime session changed."""
        orphaned_ui_ids: list[str] = []
        for tool_id, _tool_dir, _manifest in self._owner_runtime_ui_tools(
            runtime_owner_tool_id
        ):
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
