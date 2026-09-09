"""Decision and dispatch owner for governed source-code changes."""

from __future__ import annotations

from typing import Any

from governance_rule.codex import GOVERNANCE_CODEX


_DECLARATION = next(
    (item for item in GOVERNANCE_CODEX.sovereigns if item.area == "system-programming"),
    None,
)


class SystemProgrammingSovereign:
    """Allows subordinate modules to invoke approved programming tools."""

    ROLE = "system-programming-sovereign"

    def __init__(self, app: Any) -> None:
        if _DECLARATION is None:
            raise RuntimeError("system programming sovereign not found in Governance Codex")
        self.app = app
        self._started = False

    async def start(self) -> dict[str, Any]:
        self._started = True
        return self.status()

    async def stop(self) -> None:
        self._started = False

    async def request_tool_execution(
        self,
        requester_module: str,
        tool_id: str,
        operation: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._started:
            return {"ok": False, "error_code": "PROGRAMMING_SOVEREIGN_NOT_READY"}
        requester = str(requester_module or "").strip()
        if not requester or requester.startswith("governance_rule"):
            return {"ok": False, "error_code": "PERMISSION_DENIED"}
        toolbox = getattr(self.app, "toolbox_service", None)
        if toolbox is None:
            return {"ok": False, "error_code": "TOOLBOX_UNAVAILABLE"}
        request = {
            "tool_id": str(tool_id),
            "operation": str(operation),
            "requester_module": requester,
            "programming_sovereign": self.ROLE,
            "payload": dict(payload or {}),
        }
        return await toolbox.request_tool_execution(request)

    def status(self) -> dict[str, Any]:
        return {
            "role": self.ROLE,
            "started": self._started,
            "duties": list(_DECLARATION.duties),
            "code_write": "delegated-to-governed-tool",
            "channel": "shared-layer",
        }


__all__ = ["SystemProgrammingSovereign"]
