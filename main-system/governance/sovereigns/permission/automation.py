"""Permission Sovereign — Automation Orchestrator Integration."""

from __future__ import annotations

from typing import Any, Optional

from .._base import SovereignBase
from core_system.permission_automation import PermissionAutomationOrchestrator


class PermissionAutomationMixin:
    """Permission automation orchestrator integration."""

    _automation: Optional[PermissionAutomationOrchestrator]
    _governance_ref: Any

    def _governance(self) -> Any:
        if self._governance_ref is not None:
            return self._governance_ref
        return getattr(self.app, "governance", None)

    async def start_automation(self) -> dict[str, Any]:
        """Start the permission automation orchestrator."""
        if self._automation is None:
            self._automation = PermissionAutomationOrchestrator(self)
        await self._automation.start()
        return {"automation": "started"}

    async def stop_automation(self) -> None:
        """Stop the permission automation orchestrator."""
        if self._automation is not None:
            await self._automation.stop()

    def get_automation_status(self) -> dict[str, Any]:
        """Get automation status."""
        if self._automation is None:
            return {"enabled": False}
        return self._automation.status()