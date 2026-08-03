from __future__ import annotations

from typing import Any

from ipc.handlers import CommandRouter


class RuntimeBootstrap:
    """Build only the main-system command surface."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def initialize_main(self) -> None:
        self.app.command_router = CommandRouter(
            self.app,
            toolbox_service=self.app.toolbox_service,
            runtime_status_service=self.app.runtime_status_service,
        )

    async def shutdown(self) -> None:
        return None
