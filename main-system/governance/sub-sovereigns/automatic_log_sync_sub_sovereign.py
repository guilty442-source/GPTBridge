"""Automatic Log Sync Sub-Sovereign — 自動日誌同步子主權（子屬同步主宰，無決策、無執行）。

法典依據:
- sovereign_id: automatic-log-sync-sub-sovereign (position 36)
- area: automatic-log-audit-synchronization
- rank: child-of-synchronization-sovereign-no-decision-no-execution
- basis: A322
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase


class AutomaticLogSyncSubSovereign(SubSovereignBase):
    """自動日誌同步子主權：自動日誌稽核同步。"""

    sovereign_id = "automatic-log-sync-sub-sovereign"
    parent_sovereign_id = "synchronization-sovereign"

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._sync_state: dict[str, Any] = {}

    def sync_automatic_log(self, log_info: dict[str, Any]) -> None:
        """同步自動日誌稽核狀態。"""
        self._sync_state = {
            "automatic_log_audit": log_info,
            "synced_at": self._iso_now(),
        }

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["sync_state"] = self._sync_state
        return base


__all__ = ["AutomaticLogSyncSubSovereign"]