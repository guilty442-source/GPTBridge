from __future__ import annotations

from pathlib import Path
from typing import Any

from ..domain.contracts import FILE_SORTER_VERSION
from .automation_service import FileSorterAutomationService


class FileSorterService:
    """Own the standalone lifecycle while toolbox commands stay tool-scoped."""

    VERSION = FILE_SORTER_VERSION

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = Path(tool_root).resolve()
        self.automation = FileSorterAutomationService(self.tool_root)

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
