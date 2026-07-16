from __future__ import annotations

from pathlib import Path
from typing import Any


class ProjectCleanerService:
    """Keep cleaner commands in its isolated toolbox runner and project boundary."""

    VERSION = "1.0.0"

    def __init__(self, tool_root: Path) -> None:
        self.tool_root = Path(tool_root).resolve()

    def owns(self, _command: str) -> bool:
        return False

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def handle(
        self, command: str, _payload: dict[str, Any], _latest: Any = None
    ) -> tuple[str, dict[str, Any]]:
        raise ValueError(f"unsupported project-cleaner capability command: {command}")
