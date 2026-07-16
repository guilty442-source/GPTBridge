from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any


def _load_automation_service() -> type:
    backend_root = Path(__file__).resolve().parents[2]
    module_path = backend_root / "automation_service.py"
    module_name = "_gptbridge_file_sorter_automation_service"
    module = sys.modules.get(module_name)
    if module is None:
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise ImportError("File Sorter automation service is unavailable")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
    service_type = getattr(module, "FileSorterAutomationService", None)
    if not isinstance(service_type, type):
        raise ImportError("File Sorter automation service class is unavailable")
    return service_type


class FileSorterService:
    """Own the standalone lifecycle while toolbox commands stay tool-scoped."""

    VERSION = "1.0.0"

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = Path(tool_root).resolve()
        self.automation = _load_automation_service()(self.tool_root)

    def owns(self, _command: str) -> bool:
        return False

    async def start(self) -> None:
        await self.automation.start()

    async def shutdown(self) -> None:
        await self.automation.stop()

    async def handle(
        self, command: str, _payload: dict[str, Any], _latest: Any = None
    ) -> tuple[str, dict[str, Any]]:
        raise ValueError(f"unsupported File Sorter capability command: {command}")
