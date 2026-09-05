"""permission directory — 權限目錄（純資料，目錄驅動）。

目錄為權限主宰發放/終止/監管之依據（A7）。僅定義明確準予之
角色 × 目標（能力邊界）對照，不含任何行為；未知一律拒絶（A10）。

角色的準予目標採 allowlist 語意：非列於此即拒絶。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final


@dataclass(frozen=True)
class DirectoryEntry:
    role: str
    target: str
    boundary: str = ""


@dataclass(frozen=True)
class PermissionDirectory:
    entries: tuple[DirectoryEntry, ...] = field(default_factory=tuple)

    def allows(self, role: str, target: str) -> bool:
        return any(
            entry.role == role and entry.target == target
            for entry in self.entries
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "entries": [
                {"role": e.role, "target": e.target, "boundary": e.boundary}
                for e in self.entries
            ]
        }


_SYSTEM_ROLES: Final[tuple[str, ...]] = (
    "system-sovereign",
    "permission-sovereign",
    "system-runtime-sub-sovereign",
    "maintenance-sovereign",
    "system-resource-sub-sovereign",
    "system-data-sub-sovereign",
    "system-integration-sub-sovereign",
    "xingcheng",
    "governance-auditor",
)


def default_permission_directory() -> PermissionDirectory:
    targets: dict[str, tuple[str, ...]] = {
        # 系統主宰：編排所有主宰之 status 觀察（A26/A27：不代決細節）
        "system-sovereign": (
            "status:system",
            "status:permission",
            "status:runtime",
            "status:maintenance",
            "status:resource",
            "status:data",
            "status:integration",
            "delegate:lifecycle-milestone",
        ),
        # 權限主宰自身：權限事務（A6），及被所有人詢問 evaluate
        "permission-sovereign": (
            "status:permission",
            "permission:issue",
            "permission:terminate",
            "permission:supervise",
            "permission:ledger-append",
        ),
        "system-runtime-sub-sovereign": (
            "status:runtime",
            "runtime:probe-process",
            "runtime:spawn-managed-process",
            "runtime:stop-managed-process",
            "runtime:runtime-integrity",
        ),
        "maintenance-sovereign": (
            "status:maintenance",
            "maintenance:health-aggregate",
            "maintenance:fault-plans",
            "maintenance:source-selfrepair",
            "maintenance:backup-manifest",
        ),
        "system-resource-sub-sovereign": (
            "status:resource",
            "resource:memory-state",
            "resource:disk-state",
            "resource:compute-state",
            "resource:model-state",
            "resource:provision-release",
        ),
        "system-data-sub-sovereign": (
            "status:data",
            "data:sql-integrity",
            "data:semantic-index-health",
            "data:version-history-health",
            "data:data-directory",
        ),
        "system-integration-sub-sovereign": (
            "status:integration",
            "integration:channel-register",
            "integration:bus-publish",
            "integration:bus-subscribe",
            "integration:iface-sync",
        ),
        "xingcheng": (
            "status:system",
            "status:permission",
            "status:runtime",
            "status:maintenance",
            "status:resource",
            "status:data",
            "status:integration",
            "xingcheng:awareness",
        ),
        "governance-auditor": (
            "status:system",
            "status:permission",
            "status:runtime",
            "status:maintenance",
            "status:resource",
            "status:data",
            "status:integration",
            "governance:audit",
        ),
    }
    entries: list[DirectoryEntry] = []
    for role in targets:
        for target in targets[role]:
            entries.append(DirectoryEntry(role=role, target=target))
    return PermissionDirectory(tuple(sorted(entries, key=lambda e: (e.role, e.target))))


__all__ = [
    "DirectoryEntry",
    "PermissionDirectory",
    "default_permission_directory",
]