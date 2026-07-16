from __future__ import annotations

from typing import Any


class UpdateCommandService:
    """Read/acknowledge hot-update state without exposing general settings."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def handle(
        self, command: str, _payload: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        if command == "settings_health_refresh":
            return "settings_health_refresh_result", {
                "ok": True,
                "global_update_plan": self.app.update_coordinator.inspect(),
            }
        if command == "settings_mark_updates_applied":
            return (
                "settings_mark_updates_applied_result",
                self.app.update_coordinator.mark_applied(),
            )
        raise ValueError(f"unsupported update command: {command}")
