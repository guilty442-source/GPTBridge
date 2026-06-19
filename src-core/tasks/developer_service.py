from __future__ import annotations

from pathlib import Path
from typing import Any


class DeveloperService:
    """Minimal developer command service for runtime stability."""

    COMMANDS = {
        "developer_prepare_sandbox",
        "developer_auto_optimize",
        "developer_phase1_integrity",
        "developer_phase2_static",
        "developer_phase3_startup",
        "developer_phase4_health",
        "developer_phase5_ai_review",
        "developer_phase6_build",
        "developer_deploy_summary",
        "developer_apply_sandbox",
    }

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    def owns(self, command: str) -> bool:
        return command in self.COMMANDS

    async def handle(self, command: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        result_event = f"{command}_result"
        if command not in self.COMMANDS:
            return result_event, {"ok": False, "message": f"Unknown developer command: {command}"}

        # Keep backend stable even when advanced developer pipeline is not enabled.
        return result_event, {
            "ok": True,
            "command": command,
            "message": "developer command acknowledged",
            "payload": payload,
        }

