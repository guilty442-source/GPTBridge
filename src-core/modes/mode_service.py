from __future__ import annotations

from typing import Any, Dict, Protocol


class ModeService(Protocol):
    COMMANDS: set[str]

    def owns(self, command: str) -> bool:
        ...

    async def handle(self, command: str, payload: Dict[str, Any], latest_ai_answer: str | None = None) -> tuple[str, Dict[str, Any]]:
        ...
