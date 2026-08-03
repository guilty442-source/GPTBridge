import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Add src-core to sys.path so absolute imports work when this is not run as a module
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[2] / "shared-layer" / "src"),
)

from governance_rule.governance_policy import (
    DEFAULT_ACTIVE_GOVERNANCE_RULES,
    GOVERNANCE_RULE_CATALOG,
)
from core_system.runtime_bootstrap import RuntimeBootstrap
from core_system.governance_runtime import MainSystemGovernance
from core_system.hot_update_service import HotUpdateService
from core_system.daily_global_cleaner_service import DailyGlobalCleanerService
from core_system.versioning import application_version
from ipc.server import run_server
from tasks.queue import TaskQueue
from tasks.toolbox_service import ToolboxService
from tasks.runtime_status_service import RuntimeStatusService


DEFAULT_START_TOOL_IDS = ("shared-layer", "local-ai")


class GPTBridgeApp:
    # Full catalog of supported governance rules for UI selection menus.
    AVAILABLE_GOVERNANCE_RULES = list(GOVERNANCE_RULE_CATALOG)

    def __init__(self) -> None:
        project_root_override = os.environ.get("GPTBRIDGE_PROJECT_ROOT")
        self.project_root = (
            Path(os.path.abspath(project_root_override))
            if project_root_override
            else Path(__file__).resolve().parents[2]
        )
        self.version = application_version(self.project_root)
        self.maintenance_ready = False
        self.toolbox_service: ToolboxService | None = None
        self.runtime_status_service: RuntimeStatusService | None = None
        self.command_router: Any | None = None
        self.core_logger: None = None
        self.governance: MainSystemGovernance | None = None
        self.task_queue: TaskQueue | None = None
        self.runtime_bootstrap = RuntimeBootstrap(self)
        self.hot_update_service = HotUpdateService(self)
        self.daily_global_cleaner_service = DailyGlobalCleanerService(self)
        self._command_tasks: set[asyncio.Task[Any]] = set()
        self._command_task_meta: dict[asyncio.Task[Any], dict[str, Any]] = {}

        self.governance_rules_read_only = True
        self.governance_rules = self._load_governance_rules()
        self.startup_phase = "created"
        self.startup_phase_active_since = time.monotonic()
        self.startup_phase_history: list[dict[str, Any]] = []
        self._shutdown_started = False
        self._shutdown_complete = asyncio.Event()
        self.default_tool_startup: dict[str, dict[str, Any]] = {}

    def _mark_startup_phase(self, phase: str) -> None:
        now = time.monotonic()
        previous = getattr(self, "startup_phase", None)
        duration_ms = None
        if previous is not None and previous != phase:
            duration_ms = int((now - self.startup_phase_active_since) * 1000)
            self.startup_phase_history.append(
                {
                    "phase": previous,
                    "duration_ms": duration_ms,
                    "finished_at": time.time(),
                }
            )
        self.startup_phase = phase
        self.startup_phase_active_since = now
        try:
            self._log(
                {
                    "type": "startup_phase",
                    "phase": phase,
                    "duration_since_last_ms": duration_ms,
                    "timestamp": time.time(),
                }
            )
        except Exception:
            pass

    def get_startup_status(self) -> dict[str, Any]:
        now = time.monotonic()
        active_duration_ms = int((now - self.startup_phase_active_since) * 1000)
        return {
            "phase": getattr(self, "startup_phase", "unknown"),
            "phase_duration_ms": active_duration_ms,
            "phase_history": list(self.startup_phase_history),
            "maintenance_ready": self.maintenance_ready,
            "default_tools": dict(self.default_tool_startup),
            "daily_global_cleaner": self.daily_global_cleaner_service.status(),
        }

    async def _start_governed_default_tools(self) -> None:
        if self.governance is None or self.toolbox_service is None:
            return
        for tool_id in DEFAULT_START_TOOL_IDS:
            if not self.governance.can_start_tool(tool_id):
                result = {
                    "ok": False,
                    "tool_id": tool_id,
                    "error_code": "PERMISSION_DENIED",
                    "message": "PERMISSION_DENIED",
                }
            else:
                result = await self.toolbox_service.start_tool(
                    {
                        "tool_id": tool_id,
                        "request_id": f"default-start-{tool_id}-{time.time_ns()}",
                        "background": True,
                    }
                )
            self.default_tool_startup[tool_id] = {
                "ok": result.get("ok") is True,
                "runtime_mode": str(result.get("runtime_mode") or ""),
                "error_code": str(result.get("error_code") or ""),
                "message": str(result.get("message") or ""),
            }
            self._log(
                {
                    "type": "default_tool_startup",
                    "tool_id": tool_id,
                    **self.default_tool_startup[tool_id],
                }
            )

    def _load_governance_rules(self) -> list[str]:
        """Return the versioned, immutable main-system governance catalog."""

        return list(DEFAULT_ACTIVE_GOVERNANCE_RULES)

    def _normalize_global_governance_rules(self, rules: Any) -> list[str]:
        catalog = [str(item).strip() for item in self.AVAILABLE_GOVERNANCE_RULES]
        incoming = rules if isinstance(rules, list) else []
        normalized = [str(item).strip() for item in incoming if str(item).strip()]
        return list(dict.fromkeys([*catalog, *normalized]))

    def _save_governance_rules(self) -> None:
        raise PermissionError("Governance rules are immutable at runtime")

    def _log(self, data: dict[str, Any]) -> None:
        print(json.dumps(data, ensure_ascii=False), flush=True)

    async def initialize(self) -> None:
        """Initialize the mother process with lifecycle and update capabilities only."""

        project_root = self.project_root
        if self.governance is None:
            self.governance = MainSystemGovernance.from_environment(project_root)
        if self.task_queue is None:
            self.task_queue = TaskQueue(project_root, self.core_logger)
        if self.toolbox_service is None:
            self.toolbox_service = ToolboxService(
                project_root,
                governance=self.governance,
            )
        if self.runtime_status_service is None:
            self.runtime_status_service = RuntimeStatusService(self)

        await self.runtime_bootstrap.initialize_main()
        self.maintenance_ready = True
        await self._start_governed_default_tools()
        await self.daily_global_cleaner_service.start()
        self._mark_startup_phase("main_runtime_ready")
        self._log({"type": "status", "status": "ready"})

    async def shutdown(self) -> None:
        if self._shutdown_started:
            await self._shutdown_complete.wait()
            return
        self._shutdown_started = True
        try:
            await self._shutdown_once()
        finally:
            self._shutdown_complete.set()

    async def _shutdown_once(self) -> None:
        await self.daily_global_cleaner_service.stop()
        await self.hot_update_service.stop()

        pending_tasks = [task for task in self._command_tasks if not task.done()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            _done, still_running = await asyncio.wait(
                pending_tasks,
                timeout=10,
            )
            if still_running:
                self._log(
                    {
                        "type": "warning",
                        "message": (
                            f"{len(still_running)} command task(s) retained "
                            "durable recovery state after shutdown deadline"
                        ),
                    }
                )

        self._command_tasks.clear()
        self._command_task_meta.clear()
        await self.runtime_bootstrap.shutdown()
        if self.governance is not None:
            self.governance.close()
            self.governance = None


async def main() -> None:
    app_instance = GPTBridgeApp()
    parser = argparse.ArgumentParser(description="GPTBridge Mother Tool Entry")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the IPC server for the mother tool.",
    )
    parser.add_argument(
        "--auto-kill-backend-port",
        action="store_true",
        help="Automatically terminate a previous GPTBridge backend holding the configured IPC port before starting.",
    )

    args = parser.parse_args()

    try:
        await run_server(
            app_instance,
            auto_kill_backend_port=args.auto_kill_backend_port,
        )
    finally:
        await app_instance.shutdown()


if __name__ == "__main__":
    try:
        from startup import run_cli

        run_cli()
    except (KeyboardInterrupt, SystemExit):
        sys.exit(0)
