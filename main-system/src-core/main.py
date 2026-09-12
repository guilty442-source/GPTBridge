import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
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

# Import new governance architecture sovereigns
from governance.sovereigns import (
    DecisionSovereign,
    PermissionSovereign,
    SystemRuntimeSovereign,
    SynchronizationSovereign,
    XingchengSovereign,
)
from governance.sub_sovereigns import (
    SystemSubSovereign,
    StartupSubSovereign,
    LanguageReviewSubSovereign,
    DirectorySubSovereign,
    IdentityGroupSubSovereign,
    ResourceDependencySyncSubSovereign,
    ChannelContractSyncSubSovereign,
    PolicyArchitectureSubSovereign,
    HealthMaintenanceTestSubSovereign,
    DataGovernanceSubSovereign,
    PriorityCapabilitySubSovereign,
    ChangeAcceptanceSubSovereign,
    DependencySyncSubSovereign,
    ReleaseUpdateSyncSubSovereign,
    RuntimeStateSyncSubSovereign,
    RepairBackupSyncSubSovereign,
    CleanupRetentionSyncSubSovereign,
    LearningEvidenceSyncSubSovereign,
    AutomaticLogSyncSubSovereign,
)


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

        # New governance architecture sovereigns (A63/A64/A12/A128)
        # Decision layer sovereigns
        self.decision_sovereign = DecisionSovereign(self)
        self.permission_sovereign = PermissionSovereign(self)
        self.system_runtime_sovereign = SystemRuntimeSovereign(self)
        self.synchronization_sovereign = SynchronizationSovereign(self)
        self.xingcheng_sovereign = XingchengSovereign(self)

        # Sub-sovereigns (initialized on demand, parent set via set_parent)
        self._sub_sovereigns: dict[str, Any] = {}

        self.hot_reload_watcher: Any | None = None
        self._command_tasks: set[asyncio.Task[Any]] = set()
        self._command_task_meta: dict[asyncio.Task[Any], dict[str, Any]] = {}
        # A67 connection counters.  ``_active_ws_connections`` tracks any open
        # WebSocket socket (for the connection watchdog).  The independent
        # ``_authenticated_ipc_connections`` counter is incremented ONLY for
        # sockets that passed ``_websocket_request_authorized`` in the
        # handshake — it is the verified channel the readiness gate consults
        # for condition 4 (authenticated-ipc-connected), not an inference
        # from the session token.
        self._active_ws_connections: int = 0
        self._authenticated_ipc_connections: int = 0

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
                    "finished_at": datetime.now(timezone.utc).isoformat(),
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
                    "timestamp": datetime.now(timezone.utc).isoformat(),
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
            # New governance architecture status
            "decision_sovereign": self.decision_sovereign.live_status(),
            "permission_sovereign": self.permission_sovereign.coordination_status(),
            "system_runtime_sovereign": self.system_runtime_sovereign.live_status(),
            "synchronization_sovereign": self.synchronization_sovereign.live_status(),
            "xingcheng_sovereign": self.xingcheng_sovereign.live_status(),
            "sub_sovereigns": {
                name: sov.live_status() for name, sov in self._sub_sovereigns.items()
            },
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
            "at": datetime.now(timezone.utc).isoformat(),
        }
        self.startup_failures.append(failure)
        self._log({"type": "startup_failure", **failure})

    async def initialize(self) -> None:
        """Initialize the mother process via the certified startup DAG.

        A192/E167: the backend startup sequence is owned by the startup
        sovereign executor — the certified manifest declares the phase
        order, per-phase budgets, and the single monotonic deadline
        (P110/E173).  Required-phase failure aborts the generation and
        reverse-cleans activated nodes; core-ready produces a
        proof-bound handoff to the system-runtime sovereign (A194/E169).

        Phase architecture (A192 - 3 capabilities):
        - CAPABILITY 1 (boot_core): phases 0-5 (bootstrap + dependency DAG)
        - CAPABILITY 2 (startup_executor): phase 6 + readiness handoff
        - CAPABILITY 3 (startup_executor): reverse cleanup on failure

        If GPTBRIDGE_STARTUP_STATE is set by boot_core, phases 0-5 are
        already complete; we only run CAPABILITY 2 (startup_executor).
        Otherwise (standalone mode), we run the full sequence.
        """

        # A40/E26: the launcher attestation is consumed at first valid load —
        # the governance authority bootstrap precedes the certified phase DAG.
        if self.governance is None:
            self.governance = MainSystemGovernance.from_environment(self.project_root)

        # Check if boot_core has already completed phases 0-5
        startup_state = os.environ.get("GPTBRIDGE_STARTUP_STATE", "")
        generation_id = os.environ.get("GPTBRIDGE_STARTUP_GENERATION", "")

        # A128/A130: Sovereign stack startup sequence
        # 1. Peer sovereigns (learning, programming, cleaner) — parallel
        # 2. Permission sovereign (read-only)
        # 3. Maintenance sovereign + self-maintenance — parallel
        # 4. System sovereign + 6 sub-sovereigns — parallel

        self._mark_startup_phase("sovereign_stack_starting")

        # Start permission sovereign (read-only coordination face)
        await self.permission_sovereign.start()

        # Start system runtime sovereign (will start its sub-sovereigns)
        await self.system_runtime_sovereign.start()

        # Start synchronization sovereign
        await self.synchronization_sovereign.start()

        # Start xingcheng sovereign
        await self.xingcheng_sovereign.start()

        # Start decision sovereign (orchestrates the stack)
        await self.decision_sovereign.start()

        # Check if boot_core has already completed phases 0-5
        if startup_state in ("READY", "DEGRADED"):
            # CAPABILITY 1 already complete — run CAPABILITY 2 only
            self._mark_startup_phase("capability-2-startup-executor")
            from core_system.startup_executor import StartupSovereignExecutor

            executor = StartupSovereignExecutor(self)
            # Inject the generation ID from boot_core for continuity
            result = await executor.run(generation_id=generation_id)
            startup_ok = result.ok
            if not startup_ok:
                self._record_startup_failure(
                    result.failure_phase or "startup-generation",
                    RuntimeError(
                        ";".join(result.violations)
                        or next(
                            (p.error for p in result.phases if p.error),
                            "startup-generation-failed",
                        )
                    ),
                )
                # E155 PARTIAL-READY:none — a failed generation must not start
                # post-handoff runtime duties (watchers, hot-update, isolation
                # monitor).  The listener stays up in degraded mode via
                # run_server; the executor already ran reverse cleanup and set
                # startup_dead.
                return
        else:
            # Standalone mode — run full startup sequence (CAPABILITY 1 + 2)
            self._mark_startup_phase("full-startup-sequence")
            from core_system.startup_executor import StartupSovereignExecutor

            executor = StartupSovereignExecutor(self)
            result = await executor.run()
            startup_ok = result.ok
            if not startup_ok:
                self._record_startup_failure(
                    result.failure_phase or "startup-generation",
                    RuntimeError(
                        ";".join(result.violations)
                        or next(
                            (p.error for p in result.phases if p.error),
                            "startup-generation-failed",
                        )
                    ),
                )
                return

        # Automated hot-reload watcher — requests a governed, module-scoped
        # reload through the maintenance sovereign when backend source changes
        # quiet down.  Observation is separate from decision/execution.
        self._mark_startup_phase("hot_reload_watcher_starting")
        try:
            from tasks.hot_reload_watcher import HotReloadWatcher

            self.hot_reload_watcher = HotReloadWatcher(self)
            await self.hot_reload_watcher.start()
        except Exception as error:
            self._record_startup_failure("hot_reload_watcher", error)
        self._mark_startup_phase("hot_reload_watcher_started")

        # Start the hot-update idle loop so deferred resource-holding module
        # replacements are applied automatically when the system is idle.
        try:
            await self.hot_update_service.start()
        except Exception as error:
            self._record_startup_failure("hot_update_service", error)
        # Start the tool isolation health monitor and wire crash events to
        # the state change notifier so the UI sees tool crashes immediately.
        try:
            from core_system.tool_isolation import get_isolation_manager
            iso_mgr = get_isolation_manager(self.project_root)
            notifier = getattr(self, "_state_change_notifier", None)
            loop = asyncio.get_event_loop()
            if notifier is not None:
                def _on_crash(tool_id: str, entry: Any) -> None:
                    crash_info = {
                        "pid": getattr(entry, "pid", None),
                        "restart_count": getattr(entry, "restart_count", 0),
                        "exit_code": getattr(entry, "process", None),
                    }
                    if hasattr(entry, "process") and entry.process is not None:
                        crash_info["exit_code"] = entry.process.returncode
                    notifier.push_tool_crash_event(tool_id, crash_info, loop=loop)
                iso_mgr.register_crash_callback(_on_crash)
            iso_mgr.start_monitor()
        except Exception as error:
            self._record_startup_failure("tool_isolation_monitor", error)
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
        toolbox = self.toolbox_service
        if toolbox is not None:
            for record in toolbox._load_manifest_records():
                if record.get("has_custom_ui") is not True:
                    continue
                tool_id = str(record.get("id") or "").strip()
                if not tool_id:
                    continue
                try:
                    result = await toolbox.force_close_tool(
                        {
                            "tool_id": tool_id,
                            "request_id": f"main-window-close-{tool_id}-{time.time_ns()}",
                            "reason": "main-window-closed",
                        }
                    )
                    if result.get("ok") is not True:
                        self._log(
                            {
                                "type": "warning",
                                "message": "tool backend did not exit during window shutdown",
                                "tool_id": tool_id,
                                "error_code": str(result.get("error_code") or ""),
                            }
                        )
                except Exception as error:
                    self._log(
                        {
                            "type": "warning",
                            "message": "tool backend shutdown failed during window shutdown",
                            "tool_id": tool_id,
                            "error_type": type(error).__name__,
                        }
                    )

        # Stop sub-sovereigns
        for sov in self._sub_sovereigns.values():
            try:
                await sov.stop()
            except Exception:
                pass

        # Stop sovereigns (A63/A64: decision only, execution delegated)
        await self.synchronization_sovereign.stop()
        await self.xingcheng_sovereign.stop()
        await self.system_runtime_sovereign.stop()
        await self.permission_sovereign.stop()
        await self.decision_sovereign.stop()

        await self.daily_global_cleaner_service.stop()
        await self.hot_update_service.stop()
        # Stop the tool isolation health monitor.
        try:
            from core_system.tool_isolation import get_isolation_manager
            get_isolation_manager().stop_monitor()
        except Exception:
            pass
        watcher = self.hot_reload_watcher
        if watcher is not None:
            await watcher.stop()

        # Window-backed tools are closed above before the sovereign stack is
        # stopped, so no UI-owned backend remains after application exit.

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
