from __future__ import annotations

from typing import Any


class RuntimeStatusService:
    """Read-only status for the main program.

    Cleanup, diagnosis, repair, quarantine, and recovery belong to the
    main-system central repair service (``tasks.central_repair``).
    """

    COMMANDS = {"app:get-runtime-status"}

    def __init__(self, app: Any) -> None:
        self.app = app

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def handle(
        self,
        command: str,
        _payload: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        if command == "app:get-runtime-status":
            return "app:get-runtime-status_result", self.startup_status()
        raise ValueError(f"Unknown runtime status command: {command}")

    def startup_status(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "ok": True,
            "backend": "ready",
            "version": str(getattr(self.app, "version", "0.0.0")),
            "runtime_scope": getattr(
                getattr(self.app, "command_router", None), "scope", "starting"
            ),
            "message": "runtime status ok",
        }
        get_startup_status = getattr(self.app, "get_startup_status", None)
        if callable(get_startup_status):
            result.update(get_startup_status())
        return result
