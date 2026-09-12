"""sub_sovereign — 受治理執行器契約（模組執行層之執行器官）。

本契約描述 **模組執行層** 的受治理執行器（GovernedToolRuntime），
非法典子主宰決策層。法典子主宰（A315/A322/A323/A324）為無決策、
無審查、無授權、無執行之管理角色，定義於
``main-system/governance/sub_sovereigns/``，與本契約為不同層次。

本契約中各域 channel_runtime.py 之 GovernedToolRuntime 實例為受治理
執行器，向其隸屬主宰回報通道角色、健康狀態與本地維護（清理／修復／
備份協調）結果。

  * role                   = governed-executor (tool-runtime)
  * authority              = information-management-delivery-channels-and-channel-health-and-automatic-cleanup-repair-backup
  * scope                  = all-owned-channel-delivery-health-and-local-maintenance-duties
  * subordinate_to         = parent-sovereign (經由資訊層)
  * execution              = true (受治理執行器；執行權歸執行層)
  * decision               = false (不涉入主宰決策層)

注意：下列角色常數中，部分對應之法典身分已退役（A302–A310/A322/A323）。
退役常數保留為相容性別名，活躍路徑應使用法典現行正典身分。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable


SUB_SOVEREIGN_ROLE: str = "tool-runtime-sub-sovereign"

SUB_SOVEREIGN_AUTHORITY: str = (
    "information-management-delivery-channels-and-channel-health"
    "-and-automatic-cleanup-repair-backup"
)

SUB_SOVEREIGN_SCOPE: str = (
    "all-owned-channel-delivery-health-and-local-maintenance-duties"
)

SUB_SOVEREIGN_DUTY: tuple[str, ...] = (
    "information-delivery-channels",
    "channel-health",
    "automatic-cleanup",
    "automatic-repair",
    "automatic-backup-coordination",
)

# 受治理執行器隸屬各主宰；可於多個主宰下承載執行職責。
# 注意：「maintenance」對應之法典身分已退役（A302），保留僅為相容性。
SUB_SOVEREIGN_UNDER: tuple[str, ...] = ("system", "maintenance")


# ---------------------------------------------------------------------------
# 系統主宰底下之受治理執行器角色常數。
# 注意：下列角色 ID 已退役（A304-A310/A322/A323），法典現行正典身分
#   system-data-sub-sovereign      → data-governance-sub-sovereign
#   system-integration-sub-sovereign → channel-contract-sync-sub-sovereign
#   system-resource-sub-sovereign  → resource-dependency-sync-sub-sovereign
#   system-runtime-sub-sovereign   → runtime-state-sync-sub-sovereign
#   system-language-review-sub-sovereign → language-review-sub-sovereign
#   system-third-party-sub-sovereign → dependency-sync-sub-sovereign
# 常數保留為相容性別名；活躍路徑應使用法典現行正典身分。
# ---------------------------------------------------------------------------

SYSTEM_RUNTIME_ROLE: str = "system-runtime-sub-sovereign"
SYSTEM_RUNTIME_AUTHORITY: str = "runtime"
SYSTEM_RUNTIME_SCOPE: str = "runtime-duties"
SYSTEM_RUNTIME_DUTY: tuple[str, ...] = (
    "process-survival",
    "service-maintenance",
    "runtime-integrity",
)
SYSTEM_RUNTIME_UNDER: tuple[str, ...] = ("system",)


SYSTEM_RESOURCE_ROLE: str = "system-resource-sub-sovereign"
SYSTEM_RESOURCE_AUTHORITY: str = "resource"
SYSTEM_RESOURCE_SCOPE: str = "resource-duties"
SYSTEM_RESOURCE_DUTY: tuple[str, ...] = (
    "memory-state-monitor",
    "disk-state-monitor",
    "model-state-monitor",
    "compute-state-monitor",
    "resource-provision",
    "resource-delegate",
    "resource-release",
)
SYSTEM_RESOURCE_UNDER: tuple[str, ...] = ("system",)


SYSTEM_DATA_ROLE: str = "system-data-sub-sovereign"
SYSTEM_DATA_AUTHORITY: str = "data"
SYSTEM_DATA_SCOPE: str = "data-duties"
SYSTEM_DATA_DUTY: tuple[str, ...] = (
    "structured-data-access-spec",
    "semantic-index-access-spec",
    "version-history-access-spec",
    "consistency-check",
    "integrity-check",
    "data-directory",
)
SYSTEM_DATA_UNDER: tuple[str, ...] = ("system",)


SYSTEM_INTEGRATION_ROLE: str = "system-integration-sub-sovereign"
SYSTEM_INTEGRATION_AUTHORITY: str = "integration"
SYSTEM_INTEGRATION_SCOPE: str = "integration-duties"
SYSTEM_INTEGRATION_DUTY: tuple[str, ...] = (
    "cross-sovereign-structural-interface",
    "cross-module-structural-interface",
    "channel-coordination",
    "sync-mechanism",
    "bus-coordination",
)
SYSTEM_INTEGRATION_UNDER: tuple[str, ...] = ("system",)


SYSTEM_LANGUAGE_REVIEWER_ROLE: str = "system-language-review-sub-sovereign"
SYSTEM_LANGUAGE_REVIEWER_AUTHORITY: str = "programming-language-review"
SYSTEM_LANGUAGE_REVIEWER_SCOPE: str = "programming-language-review-duties"
SYSTEM_LANGUAGE_REVIEWER_DUTY: tuple[str, ...] = (
    "language-conformance-review",
    "language-acceptance-review",
    "language-migration-review",
)
SYSTEM_LANGUAGE_REVIEWER_UNDER: tuple[str, ...] = ("system",)


SYSTEM_THIRD_PARTY_MANAGER_ROLE: str = "system-third-party-sub-sovereign"
SYSTEM_THIRD_PARTY_MANAGER_AUTHORITY: str = "third-party-software-management"
SYSTEM_THIRD_PARTY_MANAGER_SCOPE: str = "third-party-software-management-duties"
SYSTEM_THIRD_PARTY_MANAGER_DUTY: tuple[str, ...] = (
    "third-party-introduction-review",
    "third-party-version-management",
    "third-party-license-review",
    "third-party-security-review",
)
SYSTEM_THIRD_PARTY_MANAGER_UNDER: tuple[str, ...] = ("system",)


# ---------------------------------------------------------------------------
# 權限主宰底下之受治理執行器角色常數。
# 注意：下列角色 ID 為執行層角色，非法典子主宰身分。法典權限主宰底下
# 之子主宰為 language-review / directory / identity-group（A316/A317）。
# 常數保留為相容性別名。
# ---------------------------------------------------------------------------

PERMISSION_SUPERVISOR_ROLE: str = "permission-supervisor-sub-sovereign"
PERMISSION_SUPERVISOR_AUTHORITY: str = "permission-supervision"
PERMISSION_SUPERVISOR_SCOPE: str = "permission-supervision-duties"
PERMISSION_SUPERVISOR_DUTY: tuple[str, ...] = (
    "permission-state-monitor",
    "permission-usage-monitor",
    "permission-issue-review",
)
PERMISSION_SUPERVISOR_UNDER: tuple[str, ...] = ("permission",)


PERMISSION_GRANTER_ROLE: str = "permission-granter-sub-sovereign"
PERMISSION_GRANTER_AUTHORITY: str = "permission-issue"
PERMISSION_GRANTER_SCOPE: str = "permission-issue-duties"
PERMISSION_GRANTER_DUTY: tuple[str, ...] = (
    "permission-issuance",
    "permission-id-assignment",
)
PERMISSION_GRANTER_UNDER: tuple[str, ...] = ("permission",)


PERMISSION_REVOKER_ROLE: str = "permission-revoker-sub-sovereign"
PERMISSION_REVOKER_AUTHORITY: str = "permission-termination"
PERMISSION_REVOKER_SCOPE: str = "permission-termination-duties"
PERMISSION_REVOKER_DUTY: tuple[str, ...] = (
    "permission-revocation",
    "permission-entitlement-recall",
)
PERMISSION_REVOKER_UNDER: tuple[str, ...] = ("permission",)


# ---------------------------------------------------------------------------
# 維護主宰底下之受治理執行器角色常數。
# 注意：maintenance-sovereign 已退役（A302），其維護職責移至
# health-maintenance-test-sub-sovereign（決策主宰底下）及對應的同步子主宰
# （cleanup-retention / repair-backup / release-update 等，A322/A323）。
# 常數保留為相容性別名；活躍路徑應使用法典現行正典身分。
# ---------------------------------------------------------------------------

MAINTENANCE_CLEANER_ROLE: str = "maintenance-cleaner-sub-sovereign"
MAINTENANCE_CLEANER_AUTHORITY: str = "automatic-cleanup"
MAINTENANCE_CLEANER_SCOPE: str = "automatic-cleanup-duties"
MAINTENANCE_CLEANER_DUTY: tuple[str, ...] = (
    "temp-file-cleanup",
    "cache-cleanup",
    "empty-directory-cleanup",
)
MAINTENANCE_CLEANER_UNDER: tuple[str, ...] = ("maintenance",)


MAINTENANCE_BACKER_ROLE: str = "maintenance-backer-sub-sovereign"
MAINTENANCE_BACKER_AUTHORITY: str = "automatic-backup"
MAINTENANCE_BACKER_SCOPE: str = "automatic-backup-duties"
MAINTENANCE_BACKER_DUTY: tuple[str, ...] = (
    "backup-coordination",
    "backup-integrity-presentation",
)
MAINTENANCE_BACKER_UNDER: tuple[str, ...] = ("maintenance",)


MAINTENANCE_REPAIRER_ROLE: str = "maintenance-repairer-sub-sovereign"
MAINTENANCE_REPAIRER_AUTHORITY: str = "automatic-repair"
MAINTENANCE_REPAIRER_SCOPE: str = "automatic-repair-duties"
MAINTENANCE_REPAIRER_DUTY: tuple[str, ...] = (
    "damage-isolation",
    "repair-execution",
    "quarantine-management",
)
MAINTENANCE_REPAIRER_UNDER: tuple[str, ...] = ("maintenance",)


MAINTENANCE_UPDATER_ROLE: str = "maintenance-updater-sub-sovereign"
MAINTENANCE_UPDATER_AUTHORITY: str = "automatic-update"
MAINTENANCE_UPDATER_SCOPE: str = "automatic-update-duties"
MAINTENANCE_UPDATER_DUTY: tuple[str, ...] = (
    "update-management",
    "update-application",
)
MAINTENANCE_UPDATER_UNDER: tuple[str, ...] = ("maintenance",)


MAINTENANCE_HEALTH_MONITOR_ROLE: str = "maintenance-health-monitor-sub-sovereign"
MAINTENANCE_HEALTH_MONITOR_AUTHORITY: str = "health-monitoring"
MAINTENANCE_HEALTH_MONITOR_SCOPE: str = "health-monitoring-duties"
MAINTENANCE_HEALTH_MONITOR_DUTY: tuple[str, ...] = (
    "system-health-monitoring",
    "health-status-presentation",
    "health-event-notification",
)
MAINTENANCE_HEALTH_MONITOR_UNDER: tuple[str, ...] = ("maintenance",)


@dataclass(frozen=True)
class ChannelHealth:
    """單一通道的健康快照。

    由子主宰在每次 claim/respond 後記錄；degraded 表示連續失敗超過
    閾值或上次請求失敗。
    """

    channel_id: str
    last_ok: bool = True
    last_request_at: str = ""
    consecutive_failures: int = 0
    degraded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self)}


@runtime_checkable
class SubSovereign(Protocol):
    """受治理執行器契約（模組執行層）。

    各主宰底下之受治理執行器必須實作此契約以承擔執行職責。任何執行均
    委派受治理執行器；執行器本身有下列責任：
      * 擁有並管理其通道（system / ai）之傳遞；
      * 監督通道健康（last_ok / degraded / latency）並向主宰呈現；
      * 自動清理：清除工具本地之臨時檔／快取／空目錄（tool-local 界限）；
      * 自動修復：隔離與修復受損之 sqlite／pycache 等工具本地狀態；
      * 自動備份協調：遵循治理備份政策向主宰呈報備份動態；
      * 遵從其隸屬主宰之韌性／清除／自修生命週期。

    注意：此契約描述 **模組執行層** 的受治理執行器，非法典子主宰決策層
    （A315/A322/A323/A324 之子主宰為無決策、無執行之管理角色）。
    """

    tool_id: str
    sovereign_id: str
    version: str

    def channels(self) -> tuple[str, ...]: ...

    def channel_for(self, channel_id: str) -> Any: ...

    def health_snapshot(self) -> dict[str, Any]: ...

    async def run(self) -> None: ...

    @property
    def role(self) -> str: ...


__all__ = [
    "SUB_SOVEREIGN_AUTHORITY",
    "SUB_SOVEREIGN_DUTY",
    "SUB_SOVEREIGN_ROLE",
    "SUB_SOVEREIGN_SCOPE",
    "SUB_SOVEREIGN_UNDER",
    "PERMISSION_SUPERVISOR_AUTHORITY",
    "PERMISSION_SUPERVISOR_DUTY",
    "PERMISSION_SUPERVISOR_ROLE",
    "PERMISSION_SUPERVISOR_SCOPE",
    "PERMISSION_SUPERVISOR_UNDER",
    "PERMISSION_GRANTER_AUTHORITY",
    "PERMISSION_GRANTER_DUTY",
    "PERMISSION_GRANTER_ROLE",
    "PERMISSION_GRANTER_SCOPE",
    "PERMISSION_GRANTER_UNDER",
    "PERMISSION_REVOKER_AUTHORITY",
    "PERMISSION_REVOKER_DUTY",
    "PERMISSION_REVOKER_ROLE",
    "PERMISSION_REVOKER_SCOPE",
    "PERMISSION_REVOKER_UNDER",
    "SYSTEM_DATA_AUTHORITY",
    "SYSTEM_DATA_DUTY",
    "SYSTEM_DATA_ROLE",
    "SYSTEM_DATA_SCOPE",
    "SYSTEM_DATA_UNDER",
    "SYSTEM_INTEGRATION_AUTHORITY",
    "SYSTEM_INTEGRATION_DUTY",
    "SYSTEM_INTEGRATION_ROLE",
    "SYSTEM_INTEGRATION_SCOPE",
    "SYSTEM_INTEGRATION_UNDER",
    "SYSTEM_LANGUAGE_REVIEWER_AUTHORITY",
    "SYSTEM_LANGUAGE_REVIEWER_DUTY",
    "SYSTEM_LANGUAGE_REVIEWER_ROLE",
    "SYSTEM_LANGUAGE_REVIEWER_SCOPE",
    "SYSTEM_LANGUAGE_REVIEWER_UNDER",
    "SYSTEM_RESOURCE_AUTHORITY",
    "SYSTEM_RESOURCE_DUTY",
    "SYSTEM_RESOURCE_ROLE",
    "SYSTEM_RESOURCE_SCOPE",
    "SYSTEM_RESOURCE_UNDER",
    "SYSTEM_RUNTIME_AUTHORITY",
    "SYSTEM_RUNTIME_DUTY",
    "SYSTEM_RUNTIME_ROLE",
    "SYSTEM_RUNTIME_SCOPE",
    "SYSTEM_RUNTIME_UNDER",
    "SYSTEM_THIRD_PARTY_MANAGER_AUTHORITY",
    "SYSTEM_THIRD_PARTY_MANAGER_DUTY",
    "SYSTEM_THIRD_PARTY_MANAGER_ROLE",
    "SYSTEM_THIRD_PARTY_MANAGER_SCOPE",
    "SYSTEM_THIRD_PARTY_MANAGER_UNDER",
    "MAINTENANCE_BACKER_AUTHORITY",
    "MAINTENANCE_BACKER_DUTY",
    "MAINTENANCE_BACKER_ROLE",
    "MAINTENANCE_BACKER_SCOPE",
    "MAINTENANCE_BACKER_UNDER",
    "MAINTENANCE_CLEANER_AUTHORITY",
    "MAINTENANCE_CLEANER_DUTY",
    "MAINTENANCE_CLEANER_ROLE",
    "MAINTENANCE_CLEANER_SCOPE",
    "MAINTENANCE_CLEANER_UNDER",
    "MAINTENANCE_HEALTH_MONITOR_AUTHORITY",
    "MAINTENANCE_HEALTH_MONITOR_DUTY",
    "MAINTENANCE_HEALTH_MONITOR_ROLE",
    "MAINTENANCE_HEALTH_MONITOR_SCOPE",
    "MAINTENANCE_HEALTH_MONITOR_UNDER",
    "MAINTENANCE_REPAIRER_AUTHORITY",
    "MAINTENANCE_REPAIRER_DUTY",
    "MAINTENANCE_REPAIRER_ROLE",
    "MAINTENANCE_REPAIRER_SCOPE",
    "MAINTENANCE_REPAIRER_UNDER",
    "MAINTENANCE_UPDATER_AUTHORITY",
    "MAINTENANCE_UPDATER_DUTY",
    "MAINTENANCE_UPDATER_ROLE",
    "MAINTENANCE_UPDATER_SCOPE",
    "MAINTENANCE_UPDATER_UNDER",
    "ChannelHealth",
    "SubSovereign",
]
