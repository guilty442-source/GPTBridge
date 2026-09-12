"""Cleanup Retention Sync Sub-Sovereign — 清理保留同步子主權（子屬同步主宰，無決策、無執行）。

法典依據:
- sovereign_id: cleanup-retention-sync-sub-sovereign (position 35)
- area: cleanup-retention-synchronization
- rank: child-of-synchronization-sovereign-no-decision-no-execution
- basis: A322
"""

from __future__ import annotations

from typing import Any

from ._base import SubSovereignBase


class CleanupRetentionSyncSubSovereign(SubSovereignBase):
    """清理保留同步子主權：清理保留同步。"""

    sovereign_id = "cleanup-retention-sync-sub-sovereign"
    parent_sovereign_id = "synchronization-sovereign"

    def __init__(self, app: Any | None = None, parent: Any | None = None) -> None:
        super().__init__(app, parent)
        self._sync_state: dict[str, Any] = {}

    def sync_cleanup_retention(self, cleanup_info: dict[str, Any]) -> None:
        """同步清理保留狀態。"""
        self._sync_state = {
            "cleanup_retention": cleanup_info,
            "synced_at": self._iso_now(),
        }

    def live_status(self) -> dict[str, Any]:
        base = super().live_status()
        base["sync_state"] = self._sync_state
        return base


__all__ = ["CleanupRetentionSyncSubSovereign"]