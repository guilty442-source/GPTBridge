from __future__ import annotations

import asyncio
from typing import Any

from core.paths import main_backup_root
from managers.backup_manager import BackupManager
from managers.browser_session import BrowserSessionManager
from managers.child_tool_service_registry import ChildToolServiceRegistry
from managers.provider_monitor import monitor_provider_health
from orchestrator.autonomous_coder import AutonomousCodingAgent
from orchestrator.state_machine import MultiAgentOrchestrator
from providers.chatgpt import ChatGPTProvider
from providers.gemini import GeminiProvider
from settings.service import SharedSettingsManager
from ipc.handlers import CommandRouter


class ModeManager:
    """Encapsulates backend mode initialization and router creation."""

    SAFE_MODE_ALLOWED_COMMANDS = {
        "app:agent-instruct",
        "app:agent-intervention",
        "app:run-unit-tests",
        "app:get-governance-rules",
        "app:get-mode-services-status",
        "app:list-mode-services",
        "app:set-governance-rules",
        "app:add-governance-rule",
        "app:delete-governance-rule",
        "app:update-governance-rule",
        "health_check",
        "discussion_query",
        "load_config",
        "mother_provider_status",
        "mother_startup_status",
        "mother_url_session_check",
        "save_config",
        "settings_health_refresh",
        "settings_mark_updates_applied",
        "settings_maintain_sandbox",
        "settings_backup_records",
        "settings_delete_backup",
        "settings_export_logs",
        "settings_export_error_logs",
        "settings_reset_provider_profile",
        "settings_open_system_browser",
        "settings_factory_reset",
        "toolbox_add_tool",
        "toolbox_cancel_tool_run",
        "toolbox_list_tools",
        "toolbox_open_tool_code",
        "toolbox_run_tool",
        "toolbox_save_tool_code",
        "toolbox_start_tool",
        "toolbox_stop_tool",
    }

    def __init__(self, app: Any) -> None:
        self.app = app
        self.project_root = app.project_root
        self._session_hooks_bound = False
        self._active_mode: str | None = None
        self._mode_services: dict[str, Any] = {}

    @property
    def active_mode(self) -> str | None:
        return self._active_mode

    def register_mode_service(self, name: str, service: Any) -> None:
        self._mode_services[name] = service
        if self.app.command_router is not None:
            self.app.command_router.mode_services[name] = service
        # Emit a lightweight runtime log so the UI and telemetry can observe registrations
        try:
            if hasattr(self.app, "_log"):
                self.app._log({"type": "mode_service_registered", "service": name})
        except Exception:
            pass
        try:
            if getattr(self.app, "core_logger", None) is not None:
                self.app.core_logger.info("mode", f"Registered mode service '{name}'")
        except Exception:
            pass

    def get_mode_service(self, name: str) -> Any | None:
        return self._mode_services.get(name)

    def all_mode_services(self) -> list[Any]:
        return list(self._mode_services.values())

    async def initialize_child_tool_services(self) -> None:
        registry = ChildToolServiceRegistry(self.project_root)
        for definition in registry.discover():
            attr_name = f"{definition.service_name}_service"
            service = getattr(self.app, attr_name, None)
            if service is None:
                service = registry.create_service(definition, self.project_root)
                setattr(self.app, attr_name, service)
            if hasattr(service, "start"):
                await service.start()
            self.register_mode_service(definition.service_name, service)

    def can_execute_command(self, command: str) -> bool:
        if self._active_mode == "safe":
            return command in self.SAFE_MODE_ALLOWED_COMMANDS
        return True

    def set_active_mode(self, mode_name: str) -> None:
        self._active_mode = mode_name

    async def initialize_full_mode(self, profile: str, headless: bool) -> None:
        await self.app._ensure_playwright_browsers_installed()

        if self.app.session is None:
            self.app.session = BrowserSessionManager(profile_name=profile, headless=False)

        if self.app.backup_manager is None:
            self.app.backup_manager = BackupManager(
                self.project_root,
                backup_root=main_backup_root(self.project_root),
                max_backup_count=self.app.max_backup_count,
                logger=self.app.core_logger,
            )
            cycle_val = int(self.app.auto_cycle) if self.app.auto_cycle else 1800
            await self.app.backup_manager.start_auto_backup(interval_seconds=cycle_val)

        if self.app._raw_chatgpt is None:
            self.app._raw_chatgpt = ChatGPTProvider(self.app.session)
        if self.app._raw_gemini is None:
            self.app._raw_gemini = GeminiProvider(self.app.session)

        self.app.chatgpt = self.app.LazyProviderProxy(self.app._raw_chatgpt, self.app)
        self.app.gemini = self.app.LazyProviderProxy(self.app._raw_gemini, self.app)

        if not self._session_hooks_bound and self.app.session is not None:
            self.app.session.on_initialize(self.app._raw_chatgpt.initialize_session)
            self.app.session.on_initialize(self.app._raw_gemini.initialize_session)

            def start_monitor() -> None:
                if self.app.connection_monitor_task is None:
                    self.app.connection_monitor_task = asyncio.create_task(
                        monitor_provider_health(self.app)
                    )

            self.app.session.on_initialize(start_monitor)
            self._session_hooks_bound = True

        await self.initialize_child_tool_services()

        self.app.orchestrator = MultiAgentOrchestrator(
            self.app.chatgpt,
            self.app.gemini,
            logger=self.app.core_logger,
        )

        self.app.autonomous_agent = AutonomousCodingAgent(
            self.app.chatgpt,
            self.app.gemini,
            project_agent=self.app.project_agent,
            enforcer=self.app.enforcer,
            logger=self.app.core_logger,
            toolbox_service=self.app.toolbox_service,
            developer_service=self.app.developer_service,
            rescue_service=self.app.rescue_service,
            backup_manager=self.app.backup_manager,
            history_manager=self.app.history_manager,
        )

        if self.app.design_service is None:
            from modes.design.service import DesignSubsystem

            self.app.design_service = DesignSubsystem(self.app, self.project_root)
            self.register_mode_service("design", self.app.design_service)

        settings_service = SharedSettingsManager(self.app, self.project_root)
        self.app.command_router = CommandRouter(
            app=self.app,
            session=self.app.session,
            chatgpt=self.app.chatgpt,
            gemini=self.app.gemini,
            backup_manager=self.app.backup_manager,
            history_manager=self.app.history_manager,
            orchestrator=self.app.orchestrator,
            autonomous_agent=self.app.autonomous_agent,
            toolbox_service=self.app.toolbox_service,
            developer_service=self.app.developer_service,
            rescue_service=self.app.rescue_service,
            settings_service=settings_service,
            mode_services=self._mode_services,
        )
        self.set_active_mode("full")
        # Emit a richer services-ready log for UI/telemetry with basic service metadata
        try:
            services_info: dict[str, dict[str, str]] = {}
            for name, svc in self._mode_services.items():
                info: dict[str, str] = {
                    "class": svc.__class__.__name__,
                    "module": svc.__class__.__module__,
                }
                ver = getattr(svc, "VERSION", None)
                if ver:
                    info["version"] = str(ver)
                # best-effort expose a path if service owns a workspace
                ws_root = getattr(getattr(svc, "workspace", None), "workspace_root", None)
                if ws_root is not None:
                    try:
                        info["path"] = str(ws_root)
                    except Exception:
                        pass
                services_info[name] = info

            try:
                if hasattr(self.app, "_log"):
                    self.app._log({"type": "mode_services_ready", "services": services_info})
            except Exception:
                pass

            try:
                if getattr(self.app, "core_logger", None) is not None:
                    self.app.core_logger.info("mode", f"Mode services ready: {list(self._mode_services.keys())}")
            except Exception:
                pass
        except Exception:
            pass

    async def initialize_safe_mode(self) -> None:
        self.app.command_router = CommandRouter(
            app=self.app,
            session=None,
            chatgpt=None,
            gemini=None,
            backup_manager=None,
            history_manager=self.app.history_manager,
            orchestrator=None,
            autonomous_agent=None,
            toolbox_service=self.app.toolbox_service,
            developer_service=None,
            rescue_service=self.app.rescue_service,
            settings_service=SharedSettingsManager(self.app, self.project_root),
            mode_services=self._mode_services,
        )
        self.set_active_mode("safe")
        self.app._log(
            {
                "type": "status",
                "status": "safe_mode",
                "message": "Emergency Safe Mode ready",
            }
        )
        if hasattr(self.app, "_mark_startup_phase"):
            self.app._mark_startup_phase("safe_mode_ready")
