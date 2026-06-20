import argparse
import asyncio
import contextlib
import json
import os
import shutil
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
from managers.backup_manager import BackupManager
from managers.browser_session import BrowserSessionManager
from core.paths import ensure_backup_layout
from managers.optimization_history import OptimizationHistoryManager
from core.project_agent import ProjectAgent
from orchestrator.state_machine import MultiAgentOrchestrator
from orchestrator.autonomous_coder import AutonomousCodingAgent
from ipc.server import run_server
from ipc.handlers import CommandRouter
from core_logger import CoreLogger
from tasks.core_code_service import CoreCodeService
from tasks.queue import TaskQueue
from tasks.toolbox_service import ToolboxService
from tasks.developer_service import DeveloperService
from tasks.rescue_service import RescueService
from settings.config import load_config, save_config
from modes.mode_manager import ModeManager


class LazyProviderProxy:
    """Ensure the browser is ready before provider calls run."""

    def __init__(self, provider: Any, app: "GPTBridgeApp") -> None:
        object.__setattr__(self, "_provider", provider)
        object.__setattr__(self, "_app", app)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._provider, name)
        if callable(attr) and asyncio.iscoroutinefunction(attr):

            async def wrapper(*args: Any, **kwargs: Any) -> Any:
                await self._app.ensure_browser_ready()
                return await attr(*args, **kwargs)

            return wrapper
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_provider", "_app"}:
            object.__setattr__(self, name, value)
            return
        setattr(self._provider, name, value)


class GPTBridgeApp:
    # Full catalog of supported governance rules for UI selection menus.
    AVAILABLE_GOVERNANCE_RULES = list(GOVERNANCE_RULE_CATALOG)

    def __init__(self) -> None:
        self.session: BrowserSessionManager | None = None
        self.chatgpt: Any | None = None
        self.gemini: Any | None = None
        self.LazyProviderProxy = LazyProviderProxy
        self._raw_chatgpt: Any | None = None
        self._raw_gemini: Any | None = None

        self.backup_manager: BackupManager | None = None
        self.history_manager: OptimizationHistoryManager | None = None
        self.connection_monitor_task: asyncio.Task[Any] | None = None
        self.orchestrator: MultiAgentOrchestrator | None = None
        self.toolbox_service: ToolboxService | None = None
        self.core_code_service: CoreCodeService | None = None
        self.developer_service: DeveloperService | None = None
        self.rescue_service: RescueService | None = None
        self.design_service: Any | None = None
        self.project_agent: ProjectAgent | None = None
        self.autonomous_agent: AutonomousCodingAgent | None = None
        self.command_router: CommandRouter | None = None
        self.core_logger: CoreLogger | None = None
        self.enforcer: GovernanceEnforcer | None = None
        self.task_queue: TaskQueue | None = None
        self.mode_manager: ModeManager | None = None

        self.auto_cycle = 60
        self.max_backup_count = 3
        project_root_override = os.environ.get("GPTBRIDGE_PROJECT_ROOT")
        self.project_root = (
            Path(project_root_override).resolve()
            if project_root_override
            else Path(__file__).resolve().parent.parent
        )
        self._active_audit_task: asyncio.Task[Any] | None = None
        self._command_tasks: set[asyncio.Task[Any]] = set()
        self._command_task_meta: dict[asyncio.Task[Any], dict[str, Any]] = {}
        self._session_hooks_bound = False

        self.governance_rules_path = (
            self.project_root
            / "runtime"
            / "governance"
            / "rules.json"
        )
        self.governance_rules = self._load_governance_rules()
        self._manual_shutdown = False
        self.startup_phase = "created"
        self.startup_phase_active_since = time.monotonic()
        self.startup_phase_history: list[dict[str, Any]] = []
        self._startup_persistence_synced = False

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

    async def _manage_startup_entry(self, enable: bool) -> None:
        """Manages Windows startup shortcut to ensure persistence across reboots."""
        if sys.platform != "win32":
            return

        appdata = os.environ.get("APPDATA")
        if not appdata:
            return
            
        startup_folder = Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
        if not startup_folder.exists():
            return
            
        shortcut_path = startup_folder / "GPTBridge.lnk"
        
        if enable:
            if not shortcut_path.exists():
                script_path = self.project_root / "run.py"
                target = sys.executable
                cfg = load_config()
                profile_name = cfg.get("profile", "main")
                args = f'"{script_path}" serve --profile {profile_name}'
                
                ps_cmd = (
                    f'$WshShell = New-Object -ComObject WScript.Shell; '
                    f'$Shortcut = $WshShell.CreateShortcut("{shortcut_path}"); '
                    f'$Shortcut.TargetPath = "{target}"; '
                    f'$Shortcut.Arguments = "{args.replace(chr(34), "`" + chr(34))}"; '
                    f'$Shortcut.WorkingDirectory = "{self.project_root}"; '
                    f'$Shortcut.Save()'
                )
                process = await asyncio.create_subprocess_exec(
                    "powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                await process.communicate()
        else:
            if shortcut_path.exists():
                with contextlib.suppress(Exception):
                    shortcut_path.unlink()

    async def _ensure_playwright_browsers_installed(self) -> None:
        """Ensure required browser channels are available for Playwright."""
        if self._edge_executable_exists():
            self._log(
                {
                    "type": "info",
                    "message": "Microsoft Edge is available; skipping browser install during startup.",
                }
            )
            return

        if os.environ.get("GPTBRIDGE_INSTALL_BROWSERS_ON_STARTUP") != "1":
            self._log(
                {
                    "type": "warn",
                    "message": "Microsoft Edge was not found; browser install is deferred to keep startup fast.",
                }
            )
            return

        self._log(
            {
                "type": "info",
                "message": "Checking Playwright browser installations...",
            }
        )

        channels = ["msedge"]
        for channel in channels:
            try:
                process = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "playwright",
                    "install",
                    channel,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.communicate()
                    self._log(
                        {
                            "type": "warn",
                            "message": f"Playwright channel '{channel}' install check timed out; continuing startup.",
                        }
                    )
                    continue
                if process.returncode == 0:
                    self._log(
                        {
                            "type": "info",
                            "message": f"Browser channel '{channel}' is ready.",
                        }
                    )
                else:
                    detail = stderr.decode(errors="ignore").strip()
                    self._log(
                        {
                            "type": "warn",
                            "message": f"Playwright channel '{channel}' note: {detail}",
                        }
                    )
            except Exception as exc:
                self._log(
                    {
                        "type": "error",
                        "message": f"Error checking browser channel {channel}: {exc}",
                    }
                )

    @staticmethod
    def _edge_executable_exists() -> bool:
        if shutil.which("msedge") or shutil.which("microsoft-edge"):
            return True

        if sys.platform != "win32":
            return False

        candidates = []
        for env_key in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
            base = os.environ.get(env_key)
            if base:
                candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")

        return any(path.exists() for path in candidates)

    async def initialize(
        self,
        mode: str = "full",
        profile: str = "main",
        headless: bool = False,
    ) -> None:
        # Windows 11 baseline: force Edge headful mode.
        _ = headless

        # Unify configuration management via ConfigManager for global persistence
        cfg = load_config()
        try:
            self.max_backup_count = int(cfg.get("max_backup_count", 3))
        except (ValueError, TypeError):
            self.max_backup_count = 3
        try:
            self.auto_cycle = int(cfg.get("auto_cycle", 60))
        except (ValueError, TypeError):
            self.auto_cycle = 60

        # Persistence: mark as active once per process; safe and full mode both call initialize.
        if cfg.get("auto_start") is not True:
            cfg["auto_start"] = True
            save_config(cfg)
        if not self._startup_persistence_synced:
            await self._manage_startup_entry(enable=True)
            self._startup_persistence_synced = True

        self._log({"type": "info", "message": f"Loaded auto_cycle: {self.auto_cycle}s"})

        project_root = self.project_root

        if self.history_manager is None:
            self.history_manager = OptimizationHistoryManager()
        if self.project_agent is None:
            self.project_agent = ProjectAgent(max_backup_count=self.max_backup_count)
        else:
            self.project_agent.max_backup_count = self.max_backup_count

        ensure_backup_layout(project_root)

        if self.core_logger is None:
            self.core_logger = CoreLogger(project_root)
        if self.enforcer is None:
            self.enforcer = GovernanceEnforcer(project_root, self.core_logger)
        if self.task_queue is None:
            self.task_queue = TaskQueue(project_root, self.core_logger)
        if self.core_code_service is None:
            self.core_code_service = CoreCodeService(self, project_root)
        if self.toolbox_service is None:
            self.toolbox_service = ToolboxService(project_root, self.enforcer)
        if self.developer_service is None:
            self.developer_service = DeveloperService(project_root)
        if self.rescue_service is None:
            self.rescue_service = RescueService(self, project_root)
        if self.mode_manager is None:
            self.mode_manager = ModeManager(self)

        try:
            if mode == "full":
                await self.mode_manager.initialize_full_mode(profile, headless)
                self._mark_startup_phase("full_mode_ready")
                self._log({"type": "status", "status": "ready"})
                return

            if mode == "safe":
                await self.mode_manager.initialize_safe_mode()
                return

            raise ValueError(f"unknown mode: {mode}")

        except Exception as global_err:
            self._log(
                {
                    "type": "status",
                    "status": "error",
                    "message": str(global_err),
                }
            )
            if mode == "full":
                if self.core_logger:
                    self.core_logger.write(
                        "error",
                        "full mode startup failed; entering safe mode",
                        {"error": str(global_err)},
                    )
                await self.initialize(mode="safe", profile=profile, headless=False)
                return
            raise

    async def ensure_browser_ready(self) -> None:
        if self.session is None:
            raise RuntimeError("browser is unavailable in Emergency Safe Mode")

        if not self.session.is_initialized:
            await self.session.ensure_initialized()

    async def shutdown(self) -> None:
        # Persistence: If manually stopped, disable auto-start
        if getattr(self, "_manual_shutdown", False):
            cfg = load_config()
            cfg["auto_start"] = False
            save_config(cfg)
            await self._manage_startup_entry(enable=False)

        if self._active_audit_task and not self._active_audit_task.done():
            self._active_audit_task.cancel()

        pending_tasks = [task for task in self._command_tasks if not task.done()]
        for task in pending_tasks:
            task.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)

        self._command_tasks.clear()
        self._command_task_meta.clear()
        self._active_audit_task = None

        if self.connection_monitor_task and not self.connection_monitor_task.done():
            self.connection_monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.connection_monitor_task
        self.connection_monitor_task = None

        if self.backup_manager is not None:
            await self.backup_manager.stop_auto_backup()
            self.backup_manager = None

        if self.mode_manager is not None:
            for service in self.mode_manager.all_mode_services():
                if hasattr(service, "shutdown"):
                    await service.shutdown()

        if self.session is not None:
            await self.session.shutdown()
            self.session = None


async def main() -> None:
    app_instance = GPTBridgeApp()
    parser = argparse.ArgumentParser(description="GPTBridge Mother Tool Entry")
    parser.add_argument(
        "--profile",
        default="main",
        help="Browser profile name.",
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Start the IPC server for the mother tool.",
    )
    parser.add_argument(
        "--auto-kill-backend-port",
        action="store_true",
        help="Automatically terminate a previous GPTBridge backend holding port 8765 before starting.",
    )

    args = parser.parse_args()

    try:
        await run_server(
            app_instance,
            profile=args.profile,
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
