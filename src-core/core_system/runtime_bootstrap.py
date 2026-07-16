from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any

from ipc.handlers import CommandRouter
from managers.child_tool_service_registry import ChildToolServiceRegistry
from core_system.update_command_service import UpdateCommandService


class RuntimeBootstrap:
    """Build the minimal command surface for a main or standalone process."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.capability_services: dict[str, Any] = {}

    async def initialize_main(self) -> None:
        self.app.command_router = CommandRouter(
            self.app,
            scope="main",
            toolbox_service=self.app.toolbox_service,
            runtime_status_service=self.app.runtime_status_service,
            update_service=UpdateCommandService(self.app),
        )

    async def initialize_standalone(self, tool_id: str) -> None:
        registry = ChildToolServiceRegistry(self.app.project_root)
        definitions = registry.discover(allowed_tool_ids={tool_id})
        if len(definitions) != 1:
            raise RuntimeError(
                f"Standalone tool '{tool_id}' must declare exactly one capability service."
            )

        definition = definitions[0]
        tool_root = (
            Path(self.app.project_root) / "platform_tools" / definition.tool_dir_name
        ).resolve()
        service_root = tool_root
        manifest_path = tool_root / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        capabilities = manifest.get("capabilities") or {}
        has_project_authority = any(
            isinstance(capability, dict)
            and capability.get("authority") == "project-root-only"
            for capability in capabilities.values()
        )
        if has_project_authority:
            configured_target = str(
                os.environ.get("GPTBRIDGE_CLEANER_TARGET_ROOT") or ""
            ).strip()
            if configured_target:
                target = Path(configured_target).resolve()
                host_root = Path(self.app.project_root).resolve()
                try:
                    target.relative_to(host_root)
                except ValueError as error:
                    raise RuntimeError(
                        "Cleaner target must remain inside the authorized host project."
                    ) from error
                service_root = target
        service = registry.create_service(definition, service_root)
        if hasattr(service, "start"):
            await service.start()
        self.capability_services[definition.service_name] = service
        self.app.command_router = CommandRouter(
            self.app,
            scope="standalone",
            toolbox_service=self.app.toolbox_service,
            runtime_status_service=self.app.runtime_status_service,
            capability_services=self.capability_services,
        )

    async def shutdown(self) -> None:
        for service in self.capability_services.values():
            if hasattr(service, "shutdown"):
                await service.shutdown()
        self.capability_services.clear()
