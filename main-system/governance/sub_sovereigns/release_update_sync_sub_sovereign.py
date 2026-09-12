"""Release Update Sync Sub-Sovereign — 發布更新同步子主權（子屬同步主宰，無決策、無執行）。

法典依據:
- sovereign_id: release-update-sync-sub-sovereign (position 31)
- area: system-programming
- rank: child-of-synchronization-sovereign-no-decision-no-execution
- basis: A309/A322 (retires system-programming-sovereign /
  system-programming-sub-sovereign)

The implementation was merged from the retired
``core_system.system_programming_sovereign.SystemProgrammingSovereign`` so
the active path keeps its governed tool-dispatch behavior (versions,
artifacts, certificates, hot update/reload synchronization) while operating
under the codex ``release-update-sync-sub-sovereign`` identity.
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase
from governance_rule.codex import GOVERNANCE_CODEX


_DECLARATION = next(
    (item for item in GOVERNANCE_CODEX.sovereigns if item.area == "system-programming"),
    None,
)
if _DECLARATION is None:
    raise RuntimeError("release update sync sub-sovereign not found in Governance Codex")


class ReleaseUpdateSyncSubSovereign(SubSovereignBase):
    """Allows subordinate modules to invoke approved programming tools.

    Versions / artifacts / certificates / hot update/reload synchronization
    is stewarded under the synchronization-sovereign (A322); this
    sub-sovereign holds no decision or execution power itself.
    """

    sovereign_id = "release-update-sync-sub-sovereign"
    parent_sovereign_id = "synchronization-sovereign"

    ROLE = sovereign_id

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._sync_state: dict[str, Any] = {}

    async def start(self) -> dict[str, Any]:
        self._started = True
        return self.status()

    async def stop(self) -> None:
        self._started = False

    def sync_release(self, release_info: dict[str, Any]) -> None:
        """同步發布狀態。"""
        self._sync_state = {
            "release": release_info,
            "synced_at": self._iso_now(),
        }

    async def request_tool_execution(
        self,
        requester_module: str,
        tool_id: str,
        operation: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._started:
            return {"ok": False, "error_code": "RELEASE_UPDATE_SYNC_NOT_READY"}
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
            "parent": self.parent_sovereign_id,
            "duties": list(_DECLARATION.duties),
            "code_write": "delegated-to-governed-tool",
            "channel": "shared-layer",
        }

    def live_status(self) -> dict[str, Any]:
        base = self.status()
        base["sync_state"] = self._sync_state
        return base


__all__ = ["ReleaseUpdateSyncSubSovereign"]
