"""Self-healing manager — A441 component health and auto-repair.

負責：
1. 檢測權限主宰組件健康狀態
2. 自動修復常見問題
3. 狀態恢復
4. 降級模式管理
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from governance_rule.permission_directory.directory_authority import (
    directory_authority_snapshot,
)
from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)
from governance_rule.permission_directory.code_rule_directory import code_rule_directory_snapshot

_logger = logging.getLogger("gptbridge.permission_automation")


class SelfHealingManager:
    """自我修復管理器。

    負責：
    1. 檢測權限主宰組件健康狀態
    2. 自動修復常見問題
    3. 狀態恢復
    4. 降級模式管理
    """

    def __init__(self, permission_sovereign: Any, check_interval: float = 300.0) -> None:
        self.permission_sovereign = permission_sovereign
        self.check_interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._degraded_mode = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="self-healing-manager")
        _logger.info("SelfHealingManager started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def run_once(self) -> None:
        """單次健康檢查——供 automation core 外部驅動（§1.1 自動化集中）。"""
        await self._health_check()

    async def _run_loop(self) -> None:
        while self._running:
            # Fallback private loop only runs with no automation core;
            # bound the tick so a hung check cannot freeze it silently
            # (same contract the core's wait_for wrapper gives).
            tick_deadline = max(30.0, min(600.0, float(self.check_interval) * 5))
            try:
                await asyncio.wait_for(self._health_check(), timeout=tick_deadline)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                _logger.warning(
                    "SelfHealingManager tick exceeded %.0fs deadline", tick_deadline
                )
            except Exception as e:
                _logger.error(f"Self-healing check failed: {e}")
            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

    async def _health_check(self) -> None:
        """健康檢查與自動修復。"""
        issues = []

        # 1. 檢查目錄可訪問性
        if not self._check_directory_access():
            issues.append("directory_access")

        # 2. 檢查治理連接
        if not self._check_governance_connection():
            issues.append("governance_connection")

        # 3. 檢查主權狀態
        if not self._check_sovereign_state():
            issues.append("sovereign_state")

        # 4. 檢查目錄權限
        if not self._check_directory_permissions():
            issues.append("directory_permissions")

        # 自動修復
        for issue in issues:
            await self._attempt_repair(issue)

        if issues:
            _logger.warning(f"Self-healing detected issues: {issues}")

    def _check_directory_access(self) -> bool:
        """檢查目錄可訪問性。"""
        try:
            code_rule_directory_snapshot()
            directory_authority_snapshot()
            identity_group_snapshot()
            return True
        except Exception:
            return False

    def _check_governance_connection(self) -> bool:
        """檢查治理連接 — 權限主宰必須能解析治理參考且已啟動。"""
        try:
            sovereign = self.permission_sovereign
            if not getattr(sovereign, "started", True):
                return False
            governance = getattr(sovereign, "_governance", None)
            if callable(governance):
                governance = governance()
            if governance is None:
                governance = getattr(getattr(sovereign, "app", None), "governance", None)
            return governance is not None
        except Exception:
            return False

    def _check_sovereign_state(self) -> bool:
        """檢查主權狀態。"""
        try:
            # 檢查權限主宰基本屬性
            return (
                hasattr(self.permission_sovereign, 'sovereign_id') and
                self.permission_sovereign.sovereign_id == "permission-sovereign"
            )
        except Exception:
            return False

    def _check_directory_permissions(self) -> bool:
        """檢查目錄權限。"""
        try:
            # 嘗試讀取受保護目錄
            from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
            dir_auth = directory_authority_snapshot()
            return dir_auth.authority_version_policy.current_version is not None
        except Exception:
            return False

    async def _attempt_repair(self, issue: str) -> None:
        """嘗試修復問題。"""
        _logger.warning(f"Attempting auto-repair for: {issue}")

        if issue == "directory_access":
            # 嘗試重新載入目錄
            try:
                code_rule_directory_snapshot()
                directory_authority_snapshot()
                identity_group_snapshot()
            except Exception as e:
                _logger.error(f"Failed to repair directory_access: {e}")

        elif issue == "governance_connection":
            # 觸發治理重新認證
            try:
                if hasattr(self.permission_sovereign, 're_certify'):
                    self.permission_sovereign.re_certify()
            except Exception as e:
                _logger.error(f"Failed to repair governance_connection: {e}")

        elif issue == "sovereign_state":
            # 重置主權狀態
            try:
                if hasattr(self.permission_sovereign, 're_certify'):
                    self.permission_sovereign.re_certify()
            except Exception as e:
                _logger.error(f"Failed to repair sovereign_state: {e}")

        elif issue == "directory_permissions":
            # 嘗試重新驗證目錄權限
            try:
                from governance_rule.permission_directory.directory_authority import directory_authority_snapshot
                directory_authority_snapshot()
            except Exception as e:
                _logger.error(f"Failed to repair directory_permissions: {e}")

    def is_degraded(self) -> bool:
        """檢查是否處於降級模式。"""
        return self._degraded_mode


__all__ = ["SelfHealingManager"]
