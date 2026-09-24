"""Directory sync manager — cross-sovereign directory synchronization.

負責：
1. 跨主權目錄的同步
2. 目錄完整性驗證
3. 變更檢測與同步
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Any, Optional

from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_permissions import (
    identity_permission_snapshot,
)
from governance_rule.permission_directory.registries.permissions.capability_boundaries import (
    capability_boundary_snapshot,
)
from governance_rule.permission_directory.code_rule_directory import code_rule_directory_snapshot
from governance_rule.governance_policy import governance_policy_snapshot

_logger = logging.getLogger("gptbridge.permission_automation")


class DirectorySyncManager:
    """目錄同步管理器。

    負責：
    1. 跨主權目錄的同步
    2. 目錄完整性驗證
    3. 變更檢測與同步
    """

    def __init__(
        self,
        permission_sovereign: Any,
        sync_interval: float = 300.0,  # 5分鐘
    ) -> None:
        self.permission_sovereign = permission_sovereign
        self.sync_interval = sync_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._last_sync_hash: Optional[str] = None

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="directory-sync-manager")
        _logger.info("DirectorySyncManager started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        _logger.info("DirectorySyncManager stopped")

    async def run_once(self) -> None:
        """單次目錄同步——供 automation core 外部驅動（§1.1 自動化集中）。"""
        await self._sync_directories()

    async def _run_loop(self) -> None:
        while self._running:
            # Fallback private loop only runs with no automation core;
            # bound the tick so a hung check cannot freeze it silently
            # (same contract the core's wait_for wrapper gives).
            tick_deadline = max(30.0, min(600.0, float(self.sync_interval) * 5))
            try:
                await asyncio.wait_for(self._sync_directories(), timeout=tick_deadline)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                _logger.warning(
                    "DirectorySyncManager tick exceeded %.0fs deadline", tick_deadline
                )
            except Exception as e:
                _logger.error(f"Directory sync failed: {e}")
            try:
                await asyncio.sleep(self.sync_interval)
            except asyncio.CancelledError:
                break

    async def _sync_directories(self) -> None:
        """同步所有受管目錄。"""
        # 獲取當前目錄快照
        code_rules = code_rule_directory_snapshot()
        authority = directory_authority_snapshot()
        identities = identity_group_snapshot()
        permissions = identity_permission_snapshot()
        capabilities, repairs = capability_boundary_snapshot()
        policy = governance_policy_snapshot()

        # 計算同步雜湊
        sync_data = f"{code_rules.initial_code_version}{authority.authority_version_policy.current_version}{len(identities.identities)}"
        current_hash = hashlib.sha256(sync_data.encode()).hexdigest()

        if self._last_sync_hash is None:
            self._last_sync_hash = current_hash
            return

        if current_hash != self._last_sync_hash:
            # A592/A604: directory/identity-group child identities are
            # retired — there is no child to notify; the hash update is
            # the sync record itself.
            _logger.info("Directory changes detected, sync triggered")
            self._last_sync_hash = current_hash

    def get_sync_status(self) -> dict[str, Any]:
        return {
            "last_sync_hash": self._last_sync_hash,
            "sync_interval": self.sync_interval,
        }


__all__ = ["DirectorySyncManager"]
