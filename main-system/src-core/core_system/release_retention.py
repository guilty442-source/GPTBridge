"""§10.15 版本保留階層（`star-release-retention/v1`）。

Blue-Green 與 A181 active-release pointer／ledger 已存在；本模組補上**保留
階層**（ACTIVE／PREVIOUS／ROLLBACK／ARCHIVE）的編排與稽核，作為 Cleaner
與 Updater 的共同依據。

階層規則（§10.15）：

| 階層 | 定義 | 保留規則 |
| --- | --- | --- |
| ACTIVE | 現行服務版本 | 不得刪除 |
| PREVIOUS | 上一良好版本 | 更新成功後仍保留；觀察窗通過才可降級 |
| ROLLBACK | 相容回復版本 | 版本鎖定；回復只能指向已認證且相容版本 |
| ARCHIVE | 較舊版本 | 壓縮／離線保存；依保留政策清理 |

Cleaner 規則：新版穩定（觀察窗）**且**備份驗證通過**且**回復路徑確認後，
才可把 PREVIOUS 降為 ARCHIVE；**不得以更新成功直接清除全部舊版**；所有
清理須稽核。

對 Express：''*transition_*'' 每步驟寫入 append-only 審計（`retention.jsonl`）；
純函式回傳 fail-closed，不明確禁止即不執行。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_logger = logging.getLogger("gptbridge.release_retention")

RETENTION_VERSION = "star-release-retention/v1"

# -- tier vocabulary -----------------------------------------------------------

TIER_ACTIVE = "ACTIVE"
TIER_PREVIOUS = "PREVIOUS"
TIER_ROLLBACK = "ROLLBACK"
TIER_ARCHIVE = "ARCHIVE"

VALID_TIERS = frozenset(
    {TIER_ACTIVE, TIER_PREVIOUS, TIER_ROLLBACK, TIER_ARCHIVE}
)
# 不得刪除的階層（Cleaner 規則）
_PROTECTED_TIERS = frozenset({TIER_ACTIVE, TIER_PREVIOUS})
# 觀測完成前 PREVIOUS 不得降級 ARCHIVE（觀察窗通過才可降級）
_NO_DEGRADE_BEFORE_OBSERVATION = frozenset({TIER_PREVIOUS})

# 允許的遷移：
# activate   : ARCHIVE/PREVIOUS/ROLLBACK → ACTIVE（新版本昇級＝昇階至 ACTIVE；
#              舊 ACTIVE 會被原子降為 PREVIOUS，保證永遠只有一個 ACTIVE）
# demote     : ACTIVE → PREVIOUS；PREVIOUS → ARCHIVE（需觀察窗＋備份＋回復路徑）
# rollback   : ROLLBACK → ACTIVE（回復只能指向已認證且相容版本）
_TIER_TRANSITIONS: dict[str, frozenset[str]] = {
    TIER_ACTIVE: frozenset({TIER_PREVIOUS}),     # 被新 ACTIVE 取代→PREVIOUS
    TIER_PREVIOUS: frozenset({TIER_ACTIVE, TIER_ROLLBACK, TIER_ARCHIVE}),
    TIER_ROLLBACK: frozenset({TIER_ACTIVE, TIER_ARCHIVE}),
    TIER_ARCHIVE: frozenset({TIER_ACTIVE}),      # 昇級：新版本由 ARCHIVE 昇 ACTIVE
}


def tier_transition_valid(source: str, target: str) -> bool:
    return target in _TIER_TRANSITIONS.get(source, frozenset())


@dataclass
class ReleaseRetentionEntry:
    """一份 release 的保留階層狀態（release_id 主鍵）。"""

    release_id: str
    tier: str = TIER_ARCHIVE
    application_version: str = ""
    artifact_root: str = ""
    compatible_with: list[str] = field(default_factory=list)
    activated_at: str = ""
    demoted_to_previous_at: str = ""
    archived_at: str = ""
    observation_passed_at: str = ""
    backup_verified: bool = False
    rollback_path_confirmed: bool = False
    updated_at: str = field(default_factory=lambda: _utc_iso())

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RetentionResult:
    ok: bool
    reason: str
    entry: Optional[dict[str, Any]] = None


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReleaseRetentionRegistry:
    """持久化版本保留階層登錄（atomic write；release_id 主鍵）。

    階層遷移受 :func:`tier_transition_valid` 約束，一律 fail-closed：
    - activate（升為 ACTIVE）：僅限 PREVIOUS／ROLLBACK。
    - demote AC→PR：新 ACTIVE 取代舊 ACTIVE 時發生。
    - **demote PR→AR（Cleaner）**：需三閘門全過——觀察窗通過＋備份驗證通過＋
      回復路徑確認；任何一項未過即拒絕（fail-closed，不刪除舊版）。
    - ROLLBACK：回復指向已認證且相容版本（compatible_with 記錄相容版本）。
    每次階段性變更 append 至審計 JSONL。
    """

    def __init__(
        self,
        state_path: str | Path,
        *,
        audit_path: str | Path | None = None,
    ) -> None:
        self._path = Path(state_path)
        self._audit_path = (
            Path(audit_path) if audit_path else self._path.with_suffix(".jsonl")
        )
        self._entries: dict[str, ReleaseRetentionEntry] = {}
        self._load()

    # -- persistence -----------------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        for item in data.get("releases", []):
            try:
                entry = ReleaseRetentionEntry(**item)
            except TypeError:
                continue
            self._entries[entry.release_id] = entry

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "retention_version": RETENTION_VERSION,
            "updated_at": _utc_iso(),
            "releases": [entry.as_dict() for entry in self._entries.values()],
        }
        fd, tmp = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=1)
            os.replace(tmp, self._path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _audit(
        self,
        operation: str,
        release_id: str,
        from_tier: str,
        to_tier: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        try:
            self._audit_path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(
                {
                    "timestamp": _utc_iso(),
                    "operation": operation,
                    "release_id": release_id,
                    "from_tier": from_tier,
                    "to_tier": to_tier,
                    "detail": detail or {},
                },
                ensure_ascii=False,
                sort_keys=True,
            ) + "\n"
            with self._audit_path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as error:
            _logger.warning("release-retention audit append failed: %s", error)

    # -- registration ------------------------------------------------------------

    def register(
        self,
        release_id: str,
        *,
        application_version: str = "",
        artifact_root: str = "",
        tier: str = TIER_ARCHIVE,
    ) -> RetentionResult:
        """註冊一個 release 至保留階層（預設 ARCHIVE，不得直接 ACTIVE）。"""
        if release_id in self._entries:
            return RetentionResult(False, "already-registered")
        if tier not in VALID_TIERS:
            return RetentionResult(False, f"invalid-tier:{tier}")
        if tier not in _TIER_TRANSITIONS:
            return RetentionResult(False, f"invalid-tier:{tier}")
        entry = ReleaseRetentionEntry(
            release_id=release_id,
            tier=tier,
            application_version=application_version,
            artifact_root=artifact_root,
        )
        self._entries[release_id] = entry
        self._persist()
        self._audit("register", release_id, "-", tier)
        return RetentionResult(True, "registered", entry.as_dict())

    # -- transitions -------------------------------------------------------------

    def _set_tier(
        self,
        release_id: str,
        target: str,
        operation: str,
        **fields: Any,
    ) -> RetentionResult:
        entry = self._entries.get(release_id)
        if entry is None:
            return RetentionResult(False, "release-not-registered")
        if not tier_transition_valid(entry.tier, target):
            return RetentionResult(
                False,
                f"illegal-tier:{entry.tier}->{target}",
                entry.as_dict(),
            )
        for key, value in fields.items():
            if hasattr(entry, key):
                setattr(entry, key, value)
        entry.tier = target
        entry.updated_at = _utc_iso()
        self._persist()
        self._audit(operation, release_id, entry.tier, target)
        return RetentionResult(True, operation, entry.as_dict())

    def become_active(
        self,
        release_id: str,
        *,
        compatible_with: list[str] | None = None,
    ) -> RetentionResult:
        """使 ARCHIVE／PREVIOUS／ROLLBACK 成為 ACTIVE（昇級／回復）。

        先以 compatible_with 標記相容版本，再升階。**永不允許兩個 ACTIVE**：
        若已存在其他 ACTIVE，則原子地先把它降為 PREVIOUS，再把目標昇為 ACTIVE
        （§10.15：新版本昇級成功，舊 ACTIVE 轉為 PREVIOUS 保留）。
        """
        entry = self._entries.get(release_id)
        if entry is None:
            return RetentionResult(False, "release-not-registered")
        if not tier_transition_valid(entry.tier, TIER_ACTIVE):
            return RetentionResult(
                False,
                f"illegal-tier:{entry.tier}->{TIER_ACTIVE}",
                entry.as_dict(),
            )
        incumbent = self.active_release()
        if incumbent is not None and incumbent.release_id != release_id:
            self._set_tier(
                incumbent.release_id,
                TIER_PREVIOUS,
                "demote-to-previous",
                demoted_to_previous_at=_utc_iso(),
            )
        if compatible_with is not None:
            entry.compatible_with = list(compatible_with)
        return self._set_tier(
            release_id,
            TIER_ACTIVE,
            "become-active",
            activated_at=_utc_iso(),
        )

    def demote_to_previous(
        self, release_id: str, previous_version: str = ""
    ) -> RetentionResult:
        """ACTIVE → PREVIOUS：被新 ACTIVE 取代時發生。"""
        return self._set_tier(
            release_id,
            TIER_PREVIOUS,
            "demote-to-previous",
            demoted_to_previous_at=_utc_iso(),
            application_version=previous_version,
        )

    def mark_observation_passed(self, release_id: str) -> RetentionResult:
        """Cleaner 觀察窗通過（新版穩定）。"""
        entry = self._entries.get(release_id)
        if entry is None:
            return RetentionResult(False, "release-not-registered")
        entry.observation_passed_at = _utc_iso()
        entry.updated_at = _utc_iso()
        self._persist()
        self._audit("mark-observation-passed", release_id, entry.tier, entry.tier)
        return RetentionResult(True, "observation-passed", entry.as_dict())

    def mark_backup_verified(
        self, release_id: str, verified: bool = True
    ) -> RetentionResult:
        entry = self._entries.get(release_id)
        if entry is None:
            return RetentionResult(False, "release-not-registered")
        entry.backup_verified = bool(verified)
        entry.updated_at = _utc_iso()
        self._persist()
        self._audit(
            "mark-backup-verified",
            release_id,
            entry.tier,
            entry.tier,
            {"verified": bool(verified)},
        )
        return RetentionResult(True, "backup-verified", entry.as_dict())

    def mark_rollback_path_confirmed(
        self, release_id: str, confirmed: bool = True
    ) -> RetentionResult:
        entry = self._entries.get(release_id)
        if entry is None:
            return RetentionResult(False, "release-not-registered")
        entry.rollback_path_confirmed = bool(confirmed)
        entry.updated_at = _utc_iso()
        self._persist()
        self._audit(
            "mark-rollback-path-confirmed",
            release_id,
            entry.tier,
            entry.tier,
            {"confirmed": bool(confirmed)},
        )
        return RetentionResult(True, "rollback-path-confirmed", entry.as_dict())

    def archive(self, release_id: str) -> RetentionResult:
        """Cleaner：PREVIOUS → ARCHIVE；需三閘門全過（§10.15）。

        New 版穩定（觀察窗通過）＋備份驗證通過＋回復路徑確認——三者任一
        未成立即 fail-closed 拒絕，**不得以更新成功直接清除全部舊版**。
        """
        entry = self._entries.get(release_id)
        if entry is None:
            return RetentionResult(False, "release-not-registered")
        if entry.tier != TIER_PREVIOUS:
            return RetentionResult(
                False,
                f"archive-requires-previous:{entry.tier}",
                entry.as_dict(),
            )
        if not entry.observation_passed_at:
            return RetentionResult(False, "observation-window-not-passed", entry.as_dict())
        if not entry.backup_verified:
            return RetentionResult(False, "backup-not-verified", entry.as_dict())
        if not entry.rollback_path_confirmed:
            return RetentionResult(
                False, "rollback-path-not-confirmed", entry.as_dict()
            )
        return self._set_tier(
            release_id, TIER_ARCHIVE, "archive", archived_at=_utc_iso()
        )

    def rollback_to(
        self, release_id: str, fallback_release_id: str
    ) -> RetentionResult:
        """回復：把 PREVIOUS（等候補 release）升級為 ACTIVE。

        回復只能指向已認證且相容版本；fallback（PREVIOUS）必須與現 ACTIVE
        相容（compatible_with 含現 ACTIVE）才准。
        """
        current = self._entries.get(release_id)
        fallback = self._entries.get(fallback_release_id)
        if current is None or fallback is None:
            return RetentionResult(False, "release-not-registered")
        if current.tier != TIER_ACTIVE:
            return RetentionResult(
                False, f"rollback-requires-active:{current.tier}"
            )
        compat = [
            c for c in current.compatible_with if c == fallback_release_id
        ]
        if not compat:
            return RetentionResult(
                False, "fallback-not-compatible", fallback.as_dict()
            )
        # 現 ACTIVE 降為 PREVIOUS，Fallback 升為 ACTIVE（回復動作本身亦須
        # 受新 ACTIVE 的觀察窗約束）。
        self._set_tier(
            release_id,
            TIER_PREVIOUS,
            "rollback-demote-active",
            demoted_to_previous_at=_utc_iso(),
        )
        return self._set_tier(
            fallback_release_id,
            TIER_ACTIVE,
            "rollback-to",
            activated_at=_utc_iso(),
        )

    # -- queries -------------------------------------------------------------------

    def get(self, release_id: str) -> Optional[ReleaseRetentionEntry]:
        return self._entries.get(release_id)

    def active_release(self) -> Optional[ReleaseRetentionEntry]:
        for entry in self._entries.values():
            if entry.tier == TIER_ACTIVE:
                return entry
        return None

    def previous_releases(self) -> list[ReleaseRetentionEntry]:
        return [
            entry
            for entry in self._entries.values()
            if entry.tier == TIER_PREVIOUS
        ]

    def snapshot(self) -> dict[str, Any]:
        return {
            "retention_version": RETENTION_VERSION,
            "releases": [entry.as_dict() for entry in self._entries.values()],
            "count": len(self._entries),
        }


# -- disk reconciliation ---------------------------------------------------------

_ARCHIVE_AGE_KEEP_DAYS_DEFAULT = 7.0


def _release_dirs(releases_root: Path) -> list[Path]:
    """List physical release payload dirs (``rc-*`` or with release-manifest)."""
    try:
        children = [
            item
            for item in releases_root.iterdir()
            if item.is_dir() and not item.is_symlink()
        ]
    except OSError:
        return []
    return sorted(
        item
        for item in children
        if item.name.startswith("rc-")
        or (item / "release-manifest.json").is_file()
    )


def reconcile_release_retention(
    releases_root: str | Path,
    registry: ReleaseRetentionRegistry,
    *,
    active_pointer_path: str | Path | None = None,
    archive_keep_days: float = _ARCHIVE_AGE_KEEP_DAYS_DEFAULT,
    now: float | None = None,
) -> dict[str, Any]:
    """§10.67 排程刪除——把 ``runtime/releases/`` 實體目錄對帳進保留階層。

    Fail-closed 語意：

    - 未登錄的 release 目錄一律先註冊為 PREVIOUS（受保護階層），絕不直接
      ARCHIVE——PREVIOUS→ARCHIVE 只走三閘門 ``archive()``。
    - active-release pointer 存在時其目標昇為 ACTIVE（PREVIOUS→ACTIVE 合法），
      其他 ACTIVE 由 ``become_active`` 原子降為 PREVIOUS。
    - 只有**已是 ARCHIVE**（上游管線已過三閘門）且 ``archived_at`` 超過
      ``archive_keep_days`` 的目錄才會被物理刪除；PREVIOUS 永不自動刪。
    - pointer 不存在時不昇任何 ACTIVE——全員維持 PREVIOUS，什麼都不刪。
    """
    root = Path(releases_root)
    now = time.time() if now is None else now
    result: dict[str, Any] = {
        "ok": True,
        "registered": [],
        "activated": None,
        "deleted": [],
        "kept": [],
        "errors": [],
    }
    dirs = _release_dirs(root)

    pointer_id: str | None = None
    if active_pointer_path is not None:
        try:
            payload = json.loads(
                Path(active_pointer_path).read_text(encoding="utf-8")
            )
            pointer_id = str(payload.get("release_id") or "") or None
        except (OSError, ValueError):
            pointer_id = None

    for directory in dirs:
        release_id = directory.name
        entry = registry.get(release_id)
        if entry is None:
            registered = registry.register(
                release_id,
                artifact_root=str(directory),
                tier=TIER_PREVIOUS,
            )
            if not registered.ok:
                result["errors"].append(
                    {"release_id": release_id, "reason": registered.reason}
                )
                continue
            result["registered"].append(release_id)
            entry = registry.get(release_id)
        if (
            pointer_id is not None
            and release_id == pointer_id
            and entry is not None
            and entry.tier != TIER_ACTIVE
        ):
            promoted = registry.become_active(release_id)
            if promoted.ok:
                result["activated"] = release_id
            else:
                result["errors"].append(
                    {"release_id": release_id, "reason": promoted.reason}
                )

    keep_seconds = max(0.0, float(archive_keep_days)) * 86400.0
    for directory in dirs:
        entry = registry.get(directory.name)
        if entry is None or entry.tier != TIER_ARCHIVE:
            if entry is not None:
                result["kept"].append(
                    {"release_id": directory.name, "tier": entry.tier}
                )
            continue
        archived_at = entry.archived_at or entry.updated_at
        try:
            archived_epoch = datetime.fromisoformat(
                archived_at.replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, AttributeError):
            result["kept"].append(
                {
                    "release_id": directory.name,
                    "tier": TIER_ARCHIVE,
                    "reason": "unresolvable-archived-at",
                }
            )
            continue
        if (now - archived_epoch) < keep_seconds:
            result["kept"].append(
                {
                    "release_id": directory.name,
                    "tier": TIER_ARCHIVE,
                    "reason": "within-archive-keep-window",
                }
            )
            continue
        try:
            shutil.rmtree(directory)
            result["deleted"].append(directory.name)
            registry._audit(
                "archive-purge",
                directory.name,
                TIER_ARCHIVE,
                "-",
                {"path": str(directory)},
            )
        except OSError as error:
            result["errors"].append(
                {
                    "release_id": directory.name,
                    "reason": f"delete-failed:{type(error).__name__}",
                }
            )

    result["ok"] = not result["errors"]
    return result


__all__ = [
    "RETENTION_VERSION",
    "ReleaseRetentionEntry",
    "ReleaseRetentionRegistry",
    "RetentionResult",
    "TIER_ACTIVE",
    "TIER_ARCHIVE",
    "TIER_PREVIOUS",
    "TIER_ROLLBACK",
    "VALID_TIERS",
    "reconcile_release_retention",
    "tier_transition_valid",
]