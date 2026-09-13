"""Identity group manager — A317 identity group auto-management.

負責：
1. 身份群組自動註冊/註銷
2. 衝突檢測與解決
3. 權限繼承管理
4. 群組成員同步
"""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

from governance_rule.permission_directory.registries.permissions.identity_groups import (
    identity_group_snapshot,
)

_logger = logging.getLogger("gptbridge.permission_automation")


class IdentityGroupManager:
    """身份群組自動管理器。

    負責：
    1. 身份群組自動註冊/註銷
    2. 衝突檢測與解決
    3. 權限繼承管理
    4. 群組成員同步
    """

    def __init__(self, permission_sovereign: Any) -> None:
        self.permission_sovereign = permission_sovereign
        self._group_registry: dict[str, dict] = {}

    def register_group(
        self,
        group_id: str,
        actor: str,
        capabilities: list[str],
        tool_id: str,
        metadata: Optional[dict] = None,
    ) -> bool:
        """註冊新身份群組。"""
        if not group_id or not actor:
            return False
        # 檢查衝突
        if group_id in self._group_registry:
            return False

        self._group_registry[group_id] = {
            "actor": actor,
            "capabilities": capabilities,
            "tool_id": tool_id,
            "metadata": metadata or {},
            "registered_at": datetime.now(timezone.utc),
            "active": True,
            # A317: groups absent from the sealed identity registry are
            # coordinated-only — flagged as directory drift, never
            # silently authoritative.
            "directory_registered": self._in_directory(group_id),
        }
        _logger.info(f"Registered identity group: {group_id} for {actor}")
        return True

    @staticmethod
    def _in_directory(group_id: str) -> bool:
        try:
            registered = {
                getattr(identity, "group_id", None) or getattr(identity, "identity_code", None)
                for identity in identity_group_snapshot().identities
            }
            return group_id in registered
        except Exception:
            return False

    def reconcile_with_directory(self) -> dict[str, Any]:
        """對帳本地群組登錄與封印身份目錄。

        產生 drift 報告：目錄有但本地未登錄（missing）、本地有但目錄
        沒有（unregistered）。不回寫任何一邊 — 對帳是協調層證據，
        決策屬於權限主宰。
        """
        try:
            registered = {
                getattr(identity, "group_id", None) or getattr(identity, "identity_code", None)
                for identity in identity_group_snapshot().identities
            }
            registered.discard(None)
        except Exception as e:
            return {"ok": False, "error": f"directory-unavailable: {e}"}
        local = {gid for gid, info in self._group_registry.items() if info["active"]}
        return {
            "ok": True,
            "missing_locally": sorted(registered - local),
            "unregistered_in_directory": sorted(local - registered),
            "local_active": len(local),
            "directory_registered": len(registered),
        }

    def unregister_group(self, group_id: str) -> bool:
        """註銷身份群組。"""
        if group_id in self._group_registry:
            self._group_registry[group_id]["active"] = False
            self._group_registry[group_id]["unregistered_at"] = datetime.now(timezone.utc)
            return True
        return False

    def detect_conflicts(self) -> list[dict]:
        """檢測身份群組衝突。"""
        conflicts = []
        actors = defaultdict(list)

        for group_id, info in self._group_registry.items():
            if info["active"]:
                actors[info["actor"]].append(group_id)

        for actor, groups in actors.items():
            if len(groups) > 1:
                conflicts.append({
                    "actor": actor,
                    "groups": groups,
                    "type": "duplicate_actor",
                })

        return conflicts

    def resolve_conflicts(self) -> list[dict]:
        """解決衝突（保留最新註冊的）。"""
        resolved = []
        conflicts = self.detect_conflicts()

        for conflict in conflicts:
            groups = conflict["groups"]
            # 保留最新註冊的，停用其他的
            sorted_groups = sorted(
                groups,
                key=lambda g: self._group_registry[g]["registered_at"],
                reverse=True,
            )
            for group_id in sorted_groups[1:]:
                self.unregister_group(group_id)
                resolved.append({"group_id": group_id, "action": "deactivated"})

        return resolved

    def get_group_status(self, group_id: str) -> Optional[dict]:
        return self._group_registry.get(group_id)

    def list_active_groups(self) -> list[dict]:
        return [
            {"group_id": gid, **info}
            for gid, info in self._group_registry.items()
            if info["active"]
        ]


__all__ = ["IdentityGroupManager"]
