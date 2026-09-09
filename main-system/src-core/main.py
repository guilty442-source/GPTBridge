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
from core_system.maintenance_sovereign import MaintenanceSovereign
from core_system.permission_sovereign import PermissionSovereign
from core_system.system_sovereign import SystemSovereignService
from core_system.main_system_self_maintenance import MainSystemSelfMaintenance
from core_system.versioning import application_version
from ipc.server import run_server
from tasks.queue import TaskQueue
from tasks.toolbox_service import ToolboxService
from tasks.runtime_status_service import RuntimeStatusService


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
        # 維護主宰與權限主宰為頂層主宰，由啟動核心按序啟動於系統主宰之前。
        # 系統主宰僅啟動其自身的子主宰（runtime/resource/data/integration/
        # language_review/third_party），不再啟動維護與權限。
        self.maintenance_sovereign = MaintenanceSovereign(self)
        self.permission_sovereign: PermissionSovereign | None = None
        self.system_sovereign_service = SystemSovereignService(self)
        self.main_system_self_maintenance: MainSystemSelfMaintenance | None = None
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
        self.startup_failures: list[dict[str, Any]] = []
        self.startup_dead = False

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
            "startup_failures": list(self.startup_failures),
            "startup_dead": self.startup_dead,
            "daily_global_cleaner": self.daily_global_cleaner_service.status(),
            "maintenance_sovereign": self.maintenance_sovereign.live_status(),
            "permission_sovereign": (
                self.permission_sovereign.coordination_status()
                if self.permission_sovereign is not None
                else {"enabled": False}
            ),
            "system_sovereign": self.system_sovereign_service.status(),
            "main_system_self_maintenance": (
                self.main_system_self_maintenance.status()
                if self.main_system_self_maintenance is not None
                else {"enabled": False}
            ),
        }

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

    def _record_startup_failure(self, stage: str, error: BaseException) -> None:
        """Single-fault isolation: record a stage failure and keep starting."""

        failure = {
            "stage": stage,
            "error": f"{type(error).__name__}: {error}",
            "at": time.time(),
        }
        self.startup_failures.append(failure)
        self._log({"type": "startup_failure", **failure})

    async def initialize(self) -> None:
        """Initialize the mother process with lifecycle and update capabilities only."""

        project_root = self.project_root
        if self.governance is None:
            self.governance = MainSystemGovernance.from_environment(project_root)
        if self.task_queue is None:
            self.task_queue = TaskQueue(project_root, self.core_logger)
        if self.permission_sovereign is None:
            self.permission_sovereign = PermissionSovereign(
                self,
                governance=self.governance,
            )
        if self.toolbox_service is None:
            self.toolbox_service = ToolboxService(
                project_root,
                governance=self.governance,
                permission_sovereign=self.permission_sovereign,
            )
        if self.runtime_status_service is None:
            self.runtime_status_service = RuntimeStatusService(self)

        # A67: initialize the repair coordinator to prevent duplicate repair
        # owners (boot_core watchdog + frontend restart).
        from tasks.repair_coordinator import init_repair_coordinator
        init_repair_coordinator(project_root)

        try:
            await self.runtime_bootstrap.initialize_main()
        except Exception as error:
            self._record_startup_failure("runtime_bootstrap", error)

        # ── 啟動核心按序啟動三大主宰 ──────────────────────────────
        # 1. 維護主宰 — 週期性維護、健康監控、自動修復協調
        self._mark_startup_phase("maintenance_sovereign_starting")
        try:
            from core_system.resource_maintenance import release_unused_memory

            self.resource_release = release_unused_memory

            toolbox = self.toolbox_service
            central_repair = None
            if toolbox is not None and hasattr(toolbox, "central_repair"):
                try:
                    central_repair = toolbox.central_repair()
                except Exception:
                    central_repair = None
            await self.daily_global_cleaner_service.start()
            maintenance_report = await self.maintenance_sovereign.start(
                daily_cleaner=self.daily_global_cleaner_service,
                hot_update=self.hot_update_service,
                repair_service=central_repair,
            )
            self._log(
                {
                    "type": "maintenance_sovereign_startup",
                    "role": maintenance_report.get("role", ""),
                }
            )
        except Exception as error:
            self._record_startup_failure("maintenance_sovereign", error)
        self._mark_startup_phase("maintenance_sovereign_started")

        # 2. 權限主宰 — 權限管理與授權面（唯讀協調層，無執行權）
        self._mark_startup_phase("permission_sovereign_starting")
        try:
            if self.permission_sovereign is None:
                self.permission_sovereign = PermissionSovereign(
                    self,
                    governance=self.governance,
                )
            self._log(
                {
                    "type": "permission_sovereign_startup",
                    "role": self.permission_sovereign.ROLE,
                }
            )
        except Exception as error:
            self._record_startup_failure("permission_sovereign", error)
        self._mark_startup_phase("permission_sovereign_started")

        # 3. 系統主宰 — 啟動其自身的子主宰（runtime/resource/data/
        #    integration/language_review/third_party），並協調維護與權限
        #    系統主宰與自我維護服務互不依賴，並行啟動以降低總啟動延遲。
        self._mark_startup_phase("sovereign_initializing")

        # Default governed modules (shared-layer, xingcheng) are now auto-started
        # by the Integration Sub-Sovereign during system_sovereign_service.start().
        self.main_system_self_maintenance = MainSystemSelfMaintenance(
            self.project_root,
            authentication=getattr(self.governance, "authentication", None),
        )

        async def _start_self_maintenance() -> None:
            try:
                await self.main_system_self_maintenance.start()
            except Exception as error:
                self._record_startup_failure("main_system_self_maintenance", error)

        # Startup maintenance (version compatibility + stability check) runs
        # before the system sovereign so that maintenance_ready is already true
        # when the sovereign tries to start resident tools.  No global lock is
        # used; the boolean flag is the only gate for tool status changes.
        await _start_self_maintenance()
        startup_report = getattr(self.main_system_self_maintenance, "_last_report", None)
        startup_ok = startup_report is not None and bool(startup_report.get("ok"))
        self.maintenance_ready = startup_ok
        if self.governance is not None:
            self.governance.maintenance_ready = startup_ok

        async def _start_system_sovereign() -> None:
            try:
                sovereign = await self.system_sovereign_service.start()
                self._log(
                    {
                        "type": "sovereign_startup",
                        "dependency_state": sovereign.get("dependency_state", ""),
                    }
                )
            except Exception as error:
                self._record_startup_failure("system_sovereign", error)

        await _start_system_sovereign()
        self._mark_startup_phase("sovereign_initialized")
        self._mark_startup_phase("main_runtime_ready")
        self._log({
            "type": "status",
            "status": "ready" if startup_ok else "maintenance_incomplete",
            "maintenance_ready": startup_ok,
        })

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
        if self.main_system_self_maintenance is not None:
            await self.main_system_self_maintenance.stop()
        await self.system_sovereign_service.stop()
        await self.maintenance_sovereign.stop()
        await self.daily_global_cleaner_service.stop()
        await self.hot_update_service.stop()

        # Independent tools (非常駐服務) are NOT stopped here.  They run in
        # detached process groups (CREATE_NEW_PROCESS_GROUP) so they survive
        # a main-system crash or graceful shutdown.  Only resident services
        # are stopped via the sovereigns above.  This ensures users can
        # continue using independent tools (e.g. ai-assistant, file-sorter)
        # even when the main system is restarting or repairing.

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
    parser.add_argument(
        "--profile",
        default="main",
        help="Browser profile name forwarded by run.py; accepted for compatibility but not used by the server.",
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
