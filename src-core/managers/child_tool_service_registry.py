from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tasks.child_tool_module import load_platform_tool_module


@dataclass(frozen=True)
class ChildToolServiceDefinition:
    service_name: str
    tool_dir_name: str
    package_name: str
    class_name: str


class ChildToolServiceRegistry:
    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.workspace_root = project_root / "platform_tools"

    def discover(self) -> list[ChildToolServiceDefinition]:
        if not self.workspace_root.exists():
            return []

        definitions: list[ChildToolServiceDefinition] = []
        for tool_dir in sorted(self.workspace_root.iterdir(), key=lambda item: item.name.lower()):
            if not tool_dir.is_dir():
                continue

            services_root = tool_dir / "src" / "backend" / "services"
            if not services_root.exists():
                continue

            for package_dir in sorted(
                services_root.iterdir(), key=lambda item: item.name.lower()
            ):
                if not package_dir.is_dir() or not (package_dir / "service.py").exists():
                    continue
                package_name = package_dir.name
                definitions.append(
                    ChildToolServiceDefinition(
                        service_name=package_name,
                        tool_dir_name=tool_dir.name,
                        package_name=package_name,
                        class_name=f"{self._class_prefix(package_name)}Service",
                    )
                )
        return definitions

    def create_service(
        self,
        definition: ChildToolServiceDefinition,
        project_root: Path,
    ) -> Any:
        module = load_platform_tool_module(
            self.project_root,
            definition.tool_dir_name,
            definition.package_name,
            "service",
        )
        service_class = getattr(module, definition.class_name)
        return service_class(project_root)

    @staticmethod
    def _class_prefix(package_name: str) -> str:
        parts = re.split(r"[^0-9A-Za-z]+", package_name)
        return "".join(part[:1].upper() + part[1:] for part in parts if part)
