import argparse
import asyncio
import contextlib
import json
import os
import re
import stat as stat_module
import sys
import time
from pathlib import Path
from typing import Any

# Add src-core to sys.path so absolute imports work when this is not run as a module
sys.path.insert(0, str(Path(__file__).resolve().parent))

from governance.enforcer import GovernanceEnforcer
from governance.rule_catalog import (
    DEFAULT_ACTIVE_GOVERNANCE_RULES,
    GOVERNANCE_RULE_CATALOG,
)
from core_system.runtime_bootstrap import RuntimeBootstrap
from core_system.hot_update_service import HotUpdateService
from core_system.versioning import application_version
from ipc.server import run_server
from core_logger import CoreLogger
from tasks.platform_automation import PlatformAutomationManager
from tasks.queue import TaskQueue
from tasks.toolbox_service import ToolboxService
from tasks.runtime_status_service import RuntimeStatusService
from settings.global_update_coordinator import GlobalUpdateCoordinator


class GPTBridgeApp:
    # Full catalog of supported governance rules for UI selection menus.
    AVAILABLE_GOVERNANCE_RULES = list(GOVERNANCE_RULE_CATALOG)

    def __init__(self) -> None:
        project_root_override = os.environ.get("GPTBRIDGE_PROJECT_ROOT")
        self.project_root = (
            Path(os.path.abspath(project_root_override))
            if project_root_override
            else Path(__file__).resolve().parent.parent
        )
        self.version = application_version(self.project_root)
        self.maintenance_ready = False
        self.toolbox_service: ToolboxService | None = None
        self.platform_automation: PlatformAutomationManager | None = None
        self.runtime_status_service: RuntimeStatusService | None = None
        self.command_router: Any | None = None
        self.core_logger: CoreLogger | None = None
        self.enforcer: GovernanceEnforcer | None = None
        self.task_queue: TaskQueue | None = None
        self.runtime_bootstrap = RuntimeBootstrap(self)
        self.update_coordinator = GlobalUpdateCoordinator(self.project_root)
        self.hot_update_service = HotUpdateService(self)
        self._independent_tool_startup_task: asyncio.Task[Any] | None = None
        self._startup_repair_task: asyncio.Task[Any] | None = None
        self._command_tasks: set[asyncio.Task[Any]] = set()
        self._command_task_meta: dict[asyncio.Task[Any], dict[str, Any]] = {}

        self.governance_rules_path = (
            self.project_root
            / "runtime"
            / "governance"
            / "rules.json"
        )
        self.governance_rules = self._load_governance_rules()
        self.startup_phase = "created"
        self.startup_phase_active_since = time.monotonic()
        self.startup_phase_history: list[dict[str, Any]] = []
        self._shutdown_started = False
        self._shutdown_complete = asyncio.Event()

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
        }

    def _load_governance_rules(self) -> list[str]:
        default_rules = self._normalize_global_governance_rules(
            DEFAULT_ACTIVE_GOVERNANCE_RULES
        )
        if self.governance_rules_path.exists():
            try:
                with self.governance_rules_path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if isinstance(payload, list):
                    rules = self._normalize_global_governance_rules(payload)
                    if rules != payload:
                        self.governance_rules = rules
                        self._save_governance_rules()
                    return rules
            except Exception:
                pass
        try:
            self.governance_rules_path.parent.mkdir(parents=True, exist_ok=True)
            with self.governance_rules_path.open("w", encoding="utf-8") as handle:
                json.dump(default_rules, handle, ensure_ascii=False, indent=2)
        except Exception:
            pass
        return default_rules

    def _normalize_global_governance_rules(self, rules: Any) -> list[str]:
        catalog = [str(item).strip() for item in self.AVAILABLE_GOVERNANCE_RULES]
        incoming = rules if isinstance(rules, list) else []
        normalized = [str(item).strip() for item in incoming if str(item).strip()]
        return list(dict.fromkeys([*catalog, *normalized]))

    def _save_governance_rules(self) -> None:
        self.governance_rules = self._normalize_global_governance_rules(
            self.governance_rules
        )
        self.governance_rules_path.parent.mkdir(parents=True, exist_ok=True)
        with self.governance_rules_path.open("w", encoding="utf-8") as handle:
            json.dump(self.governance_rules, handle, ensure_ascii=False, indent=2)

    def _log(self, data: dict[str, Any]) -> None:
        print(json.dumps(data, ensure_ascii=False), flush=True)

    async def initialize(self) -> None:
        """Initialize the mother process with lifecycle and update capabilities only."""

        project_root = self.project_root
        if self.core_logger is None:
            self.core_logger = CoreLogger(project_root)
        if self.enforcer is None:
            self.enforcer = GovernanceEnforcer(project_root, self.core_logger)
        if self.task_queue is None:
            self.task_queue = TaskQueue(project_root, self.core_logger)
        if self.toolbox_service is None:
            self.toolbox_service = ToolboxService(project_root, self.enforcer)
        if self.runtime_status_service is None:
            self.runtime_status_service = RuntimeStatusService(self)

        await self.runtime_bootstrap.initialize_main()
        self._mark_startup_phase("main_runtime_ready")
        self._log({"type": "status", "status": "ready"})
        update_plan = self.update_coordinator.inspect()
        self._startup_repair_task = asyncio.create_task(
            self._complete_startup_maintenance(
                update_plan=update_plan
            ),
            name="startup-maintenance",
        )

    async def _complete_startup_maintenance(
        self, *, update_plan: dict[str, Any]
    ) -> None:
        repairs_ok = False
        try:
            repairs_ok = await self._run_declared_auto_repairs(
                include_upgrade_repair=bool(update_plan.get("changed"))
            )
            if repairs_ok:
                if update_plan.get("changed"):
                    await self.hot_update_service._apply(
                        update_plan,
                        repairs_completed=True,
                    )
                else:
                    self.update_coordinator.mark_applied()
                self.maintenance_ready = True
                self.hot_update_service.start()
                if (
                    self._independent_tool_startup_task is None
                    or self._independent_tool_startup_task.done()
                ):
                    self._independent_tool_startup_task = asyncio.create_task(
                        self._start_manifest_background_tools(),
                        name="independent-tool-background-startup",
                    )
        finally:
            self._log(
                {
                    "type": "startup_maintenance_complete",
                    "ok": repairs_ok,
                    "maintenance_ready": self.maintenance_ready,
                }
            )

    async def _run_declared_auto_repairs(self, *, include_upgrade_repair: bool) -> bool:
        """Run manifest-declared repairs without importing tool business code."""

        if self.toolbox_service is None:
            return False
        tools_root = (self.project_root / "platform_tools").resolve()
        manifests = sorted(tools_root.glob("*/manifest.json"))

        async def run_repair(
            manifest_path: Path,
            capability_name: str,
            *,
            timeout_seconds: float,
        ) -> bool:
            tool_id = manifest_path.parent.name
            try:
                tool_root = manifest_path.parent.resolve()
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                tool_id = str(manifest.get("id") or "").strip()
                if manifest.get("enabled", True) is False or tool_id != tool_root.name:
                    return True
                capabilities = manifest.get("capabilities") or {}
                capability = capabilities.get(capability_name)
                if not isinstance(capability, dict):
                    raise RuntimeError(f"missing {capability_name} capability")
                entry = (tool_root / str(capability.get("entry") or "")).resolve()
                entry.relative_to(tool_root)
                if not entry.is_file():
                    raise RuntimeError(f"missing repair entry: {entry.name}")
                arguments = capability.get("arguments") or []
                if not isinstance(arguments, list) or not all(
                    isinstance(item, str) for item in arguments
                ):
                    raise RuntimeError("invalid repair arguments")
                environment = self.toolbox_service._tool_environment(
                    tool_id, tool_root, manifest
                )
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(entry),
                    *arguments,
                    cwd=str(tool_root),
                    env=environment,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(), timeout=timeout_seconds
                    )
                except asyncio.TimeoutError:
                    if process.returncode is None:
                        process.kill()
                        await process.communicate()
                    raise
                ok = process.returncode == 0
                message = (stdout if ok else stderr).decode(
                    "utf-8", errors="replace"
                ).strip()
                self.update_coordinator.repository.record_repair(
                    f"{tool_id}:{capability_name}", ok, message
                )
                return ok
            except (OSError, ValueError, RuntimeError, asyncio.TimeoutError) as error:
                self.update_coordinator.repository.record_repair(
                    f"{tool_id}:{capability_name}",
                    False,
                    f"{type(error).__name__}: {error}",
                )
                self._log(
                    {
                        "type": "tool_auto_repair_failed",
                        "tool_id": tool_id,
                        "capability": capability_name,
                        "error": type(error).__name__,
                    }
                )
                return False

        # Tool-local repair procedures have separate code and data authority,
        # so they can run concurrently without extending startup by one timeout
        # per tool.
        basic_results = await asyncio.gather(
            *(
                run_repair(
                    manifest_path,
                    "auto-repair",
                    timeout_seconds=45,
                )
                for manifest_path in manifests
            )
        )
        upgrade_results: list[bool] = []
        if include_upgrade_repair:
            for manifest_path in manifests:
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    capability = (manifest.get("capabilities") or {}).get(
                        "upgrade-repair"
                    )
                except (OSError, json.JSONDecodeError):
                    capability = None
                if isinstance(capability, dict):
                    upgrade_results.append(
                        await run_repair(
                            manifest_path,
                            "upgrade-repair",
                            timeout_seconds=90,
                        )
                    )
        return all([*basic_results, *upgrade_results])

    async def _start_manifest_background_tools(self) -> None:
        """Start explicitly opted-in independent EXEs without showing windows."""

        if self.toolbox_service is None:
            return
        tools_root = self.project_root / "platform_tools"
        if not tools_root.is_dir():
            return
        for tool_dir in sorted(tools_root.iterdir(), key=lambda item: item.name):
            manifest_path = tool_dir / "manifest.json"
            try:
                tool_stat = tool_dir.lstat()
                manifest_stat = manifest_path.lstat()
                tool_attributes = int(
                    getattr(tool_stat, "st_file_attributes", 0) or 0
                )
                manifest_attributes = int(
                    getattr(manifest_stat, "st_file_attributes", 0) or 0
                )
                if (
                    stat_module.S_ISLNK(tool_stat.st_mode)
                    or bool(tool_attributes & 0x400)
                    or not stat_module.S_ISDIR(tool_stat.st_mode)
                    or stat_module.S_ISLNK(manifest_stat.st_mode)
                    or bool(manifest_attributes & 0x400)
                    or not stat_module.S_ISREG(manifest_stat.st_mode)
                ):
                    continue
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            startup = manifest.get("startup")
            if (
                not isinstance(startup, dict)
                or startup.get("auto_start") is not True
                or startup.get("background") is not True
            ):
                continue
            tool_id = str(manifest.get("id") or "").strip()
            if tool_id != tool_dir.name:
                continue
            try:
                result = await self.toolbox_service.start_tool(
                    {
                        "tool_id": tool_id,
                        "request_id": f"startup:{tool_id}",
                        "background": True,
                    }
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                result = {
                    "ok": False,
                    "error_code": type(error).__name__,
                }
            self._log(
                {
                    "type": "independent_tool_background_start",
                    "tool_id": tool_id,
                    "ok": result.get("ok") is True,
                    "error_code": str(result.get("error_code") or ""),
                }
            )

    async def initialize_standalone_tool(self) -> None:
        """Load only the packaged child-tool service and its command router.

        Standalone EXEs must not enable the main application's startup shortcut,
        backup loop, browser stack, unrestricted toolbox, or project automation
        services.
        """

        tool_id = str(
            os.environ.get("GPTBRIDGE_STANDALONE_TOOL_ID") or ""
        ).strip()
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", tool_id):
            raise RuntimeError("Standalone tool identity is invalid.")
        tools_root = self.project_root / "platform_tools"
        tool_root = tools_root / tool_id
        manifest_path = tool_root / "manifest.json"
        for candidate, label in (
            (self.project_root, "standalone project root"),
            (tools_root, "standalone tools root"),
            (tool_root, "standalone tool root"),
            (manifest_path, "standalone tool manifest"),
        ):
            try:
                value = candidate.lstat()
            except OSError as error:
                raise RuntimeError(f"{label} is unavailable: {candidate}") from error
            attributes = int(getattr(value, "st_file_attributes", 0) or 0)
            if (
                stat_module.S_ISLNK(value.st_mode)
                or bool(attributes & 0x400)
            ):
                raise RuntimeError(f"{label} cannot be a link or reparse point.")
        if not stat_module.S_ISREG(manifest_path.lstat().st_mode):
            raise RuntimeError("Standalone tool manifest must be a regular file.")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise RuntimeError("Standalone tool manifest is invalid.") from error
        if not isinstance(manifest, dict) or str(manifest.get("id") or "") != tool_id:
            raise RuntimeError("Standalone tool manifest identity does not match.")
        tool_version = str(manifest.get("version") or "").strip()
        if not tool_version:
            raise RuntimeError("Standalone tool manifest version is missing.")

        self.toolbox_service = ToolboxService(
            self.project_root,
            allowed_tool_ids={tool_id},
        )
        if self.runtime_status_service is None:
            self.runtime_status_service = RuntimeStatusService(self)
        self.standalone_capabilities = {
            "tool_id": tool_id,
            "tool_version": tool_version,
            "toolbox": {
                "tool_id": tool_id,
                "tool_version": tool_version,
                "commands": [
                    "toolbox_run_tool",
                    "toolbox_cancel_tool_run",
                ],
            },
        }
        if self.platform_automation is None:
            self.platform_automation = PlatformAutomationManager(
                self.project_root,
                self.core_logger,
                allowed_tool_ids={tool_id},
            )
        await self.platform_automation.start()
        await self.runtime_bootstrap.initialize_standalone(tool_id)
        self._mark_startup_phase("standalone_runtime_ready")

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
        startup_repair = self._startup_repair_task
        if (
            startup_repair is not None
            and startup_repair is not asyncio.current_task()
            and not startup_repair.done()
        ):
            with contextlib.suppress(Exception):
                await startup_repair
        self._startup_repair_task = None
        await self.hot_update_service.stop()
        independent_tool_startup = self._independent_tool_startup_task
        if (
            independent_tool_startup is not None
            and independent_tool_startup is not asyncio.current_task()
            and not independent_tool_startup.done()
        ):
            independent_tool_startup.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await independent_tool_startup
        self._independent_tool_startup_task = None

        if self.platform_automation is not None:
            await self.platform_automation.stop()
            self.platform_automation = None

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
