from __future__ import annotations

from typing import Any


class HotUpdateService:
    """Version-gated main-system hot-update boundary.

    Initial-version source changes are loaded only by a fresh launcher start.
    Runtime hot update remains closed until an explicit versioned update
    command is authorized by governance.
    """

    def __init__(self, app: Any, interval_seconds: float = 1.0) -> None:
        self.app = app
        self.interval_seconds = max(0.5, interval_seconds)

    def start(self) -> None:
        raise PermissionError("PERMISSION_DENIED")

    async def stop(self) -> None:
        return None

    async def _apply(
        self,
        _plan: dict[str, Any],
        *,
        repairs_completed: bool = False,
    ) -> None:
        del repairs_completed
        raise PermissionError("PERMISSION_DENIED")
