"""星澄原生模型生命週期（``star-model-lifecycle/v1``）。

狀態機：

    UNINITIALIZED → INITIALIZED → PRETRAINING → PRETRAINED
        → SFT_TRAINING → INSTRUCT_READY → EVALUATING → READY
    READY → LOADED ⇄ UNLOADED；任何狀態可轉 FAILED，
    FAILED 可回到 INITIALIZED 重建（不覆蓋既有可用權重）。

設計規則：

- 權重、tokenizer、config、training state、evaluation report
  各自為獨立 artefact 版本紀錄，互相以 sha256 記連；
- 升級寫**新版本**、絕不覆蓋目前 ``active`` 權重；
- 狀態持久化為單一 JSON（``lifecycle.json``），原子寫入；
- 轉移非法時 fail-closed（``LIFECYCLE_TRANSITION_DENIED``）。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

LIFECYCLE_FORMAT = "star-model-lifecycle/v1"

STATES = (
    "UNINITIALIZED",
    "INITIALIZED",
    "PRETRAINING",
    "PRETRAINED",
    "SFT_TRAINING",
    "INSTRUCT_READY",
    "EVALUATING",
    "READY",
    "LOADED",
    "UNLOADED",
    "FAILED",
)

_TRANSITIONS: dict[str, frozenset[str]] = {
    "UNINITIALIZED": frozenset({"INITIALIZED"}),
    "INITIALIZED": frozenset({"PRETRAINING", "SFT_TRAINING", "FAILED"}),
    "PRETRAINING": frozenset({"PRETRAINED", "FAILED"}),
    "PRETRAINED": frozenset(
        {"SFT_TRAINING", "EVALUATING", "READY", "PRETRAINING", "FAILED"}
    ),
    "SFT_TRAINING": frozenset({"INSTRUCT_READY", "FAILED"}),
    "INSTRUCT_READY": frozenset({"EVALUATING", "READY", "SFT_TRAINING", "FAILED"}),
    "EVALUATING": frozenset({"READY", "INSTRUCT_READY", "FAILED"}),
    "READY": frozenset({"LOADED", "SFT_TRAINING", "EVALUATING", "FAILED"}),
    "LOADED": frozenset({"UNLOADED", "FAILED"}),
    "UNLOADED": frozenset({"LOADED", "READY", "FAILED"}),
    "FAILED": frozenset({"INITIALIZED"}),
}

ARTIFACT_KINDS = (
    "weights",
    "tokenizer",
    "config",
    "training_state",
    "evaluation_report",
)


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_write(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


@dataclass
class ModelLifecycle:
    """單一模型線的血統與狀態紀錄（持久化於 ``lifecycle.json``）。"""

    model_id: str
    state: str = "UNINITIALIZED"
    artifacts: dict[str, dict[str, Any]] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    active_weights_version: int = 0

    def transition(self, target: str, *, reason: str = "") -> None:
        target = str(target).upper()
        if target not in STATES:
            raise ValueError(f"LIFECYCLE_STATE_UNKNOWN:{target}")
        if target not in _TRANSITIONS[self.state]:
            raise ValueError(
                f"LIFECYCLE_TRANSITION_DENIED:{self.state}->{target}"
            )
        previous = self.state
        self.state = target
        self.history.append(
            {
                "at": _utcnow(),
                "from": previous,
                "to": target,
                "reason": str(reason),
            }
        )

    def fail(self, reason: str) -> None:
        self.transition("FAILED", reason=reason)

    def register_artifact(
        self,
        kind: str,
        path: str | Path,
        *,
        metadata: dict[str, Any] | None = None,
        activate: bool = False,
    ) -> dict[str, Any]:
        """登錄 artefact 版本；``activate=True`` 時成為當前權重（僅 weights）。

        新登錄的 weights 取得遞增版本號，**不覆蓋**既有版本紀錄——
        回滾 = 把 ``active_weights_version`` 指回舊版本。
        """
        kind = str(kind)
        if kind not in ARTIFACT_KINDS:
            raise ValueError(f"ARTIFACT_KIND_UNKNOWN:{kind}")
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"ARTIFACT_MISSING:{file_path}")
        versions = self.artifacts.setdefault(kind, {"versions": []})
        known = [
            int(e["version"])
            for e in versions["versions"]
        ] + [
            int(e["version"]) for e in versions.get("retired", [])
        ]
        entry = {
            "version": (max(known) + 1) if known else 1,
            "path": str(file_path),
            "sha256": _sha256_file(file_path),
            "registered_at": _utcnow(),
            "metadata": dict(metadata or {}),
        }
        versions["versions"].append(entry)
        if kind == "weights" and activate:
            self.active_weights_version = int(entry["version"])
        self.history.append(
            {
                "at": _utcnow(),
                "event": "artifact_registered",
                "kind": kind,
                "version": entry["version"],
                "sha256": entry["sha256"],
            }
        )
        return entry

    def rollback_weights(self, version: int) -> dict[str, Any]:
        """把 active 權重指回既有版本（不刪除任何版本）。"""
        versions = self.artifacts.get("weights", {}).get("versions", [])
        for entry in versions:
            if int(entry["version"]) == int(version):
                self.active_weights_version = int(version)
                self.history.append(
                    {
                        "at": _utcnow(),
                        "event": "weights_rollback",
                        "version": int(version),
                    }
                )
                return entry
        raise ValueError(f"WEIGHTS_VERSION_UNKNOWN:{version}")

    def rollback_target_versions(
        self,
        *,
        compat_fingerprint: str | None,
        compat_resolver: Callable[[Path], str | None] | None = None,
        exclude_versions: Iterable[int] | None = None,
        min_maturity_level: int = 1,
    ) -> list[int]:
        """2026-09-22 總督裁定 rollback 語義的合格留存版本清單。

        合格目標必須同時滿足：

        - 留存：仍在 ``versions``（未退役）且權重檔案仍存在（未 prune）；
        - 已認證：metadata 帶成熟度證據（``maturity_level`` 達下限，
          或留有 ``maturity_report``）；
        - 相容：config 指紋與錨點一致（先取 metadata ``config_sha256``，
          缺紀錄時由 ``compat_resolver`` 惰性推算；無法證明相容者
          deny-by-default 不合格）。

        ``compat_fingerprint`` 為空視為無相容錨點——一律回傳空清單
        （fail-closed，不對無錨點放行）。
        """
        if not compat_fingerprint:
            return []
        excluded = {int(v) for v in (exclude_versions or ())}
        targets: list[int] = []
        for entry in self.artifacts.get("weights", {}).get("versions", []):
            version = int(entry.get("version") or 0)
            if version in excluded:
                continue
            path = Path(str(entry.get("path") or ""))
            if not path.is_file():
                continue
            metadata = entry.get("metadata") or {}
            certified = False
            level = metadata.get("maturity_level")
            try:
                if level is not None:
                    certified = int(level) >= int(min_maturity_level)
            except (TypeError, ValueError):
                certified = False
            if not certified:
                certified = bool(
                    str(metadata.get("maturity_report") or "").strip()
                )
            if not certified:
                continue
            fingerprint = str(metadata.get("config_sha256") or "")
            if not fingerprint and compat_resolver is not None:
                fingerprint = str(compat_resolver(path) or "")
            if not fingerprint or fingerprint != str(compat_fingerprint):
                continue
            targets.append(version)
        return sorted(targets)

    def governed_rollback_weights(
        self,
        version: int,
        *,
        compat_fingerprint: str | None,
        compat_resolver: Callable[[Path], str | None] | None = None,
        exclude_versions: Iterable[int] | None = None,
        min_maturity_level: int = 1,
    ) -> dict[str, Any]:
        """受閘 rollback（2026-09-22 總督裁定）：目標須為已認證且相容之
        留存版本；不合格或無留存版本時 fail-closed 拒絕
        （``WEIGHTS_ROLLBACK_DENIED``），active 指標不變。"""
        targets = self.rollback_target_versions(
            compat_fingerprint=compat_fingerprint,
            compat_resolver=compat_resolver,
            exclude_versions=exclude_versions,
            min_maturity_level=min_maturity_level,
        )
        if int(version) not in targets:
            raise ValueError(f"WEIGHTS_ROLLBACK_DENIED:{int(version)}")
        entry = self.rollback_weights(version)
        self.history[-1]["gate"] = "star-rollback-gate/v1"
        return entry

    def active_weights(self) -> dict[str, Any] | None:
        for entry in self.artifacts.get("weights", {}).get("versions", []):
            if int(entry["version"]) == self.active_weights_version:
                return entry
        return None

    def retire_weights(
        self,
        keep_latest: int = 1,
        *,
        extra_keep_paths: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """§10.67 世代淘汰——把超出保留窗的 weights 版本移入 ``retired`` 子表。

        保留規則：最新 ``keep_latest`` 個版本**加上**當前 active 版本（回滾後
        active 可能不是最新）以及 ``extra_keep_paths`` 指向的版本
        （如 ``native-engine.json`` 釘定的 checkpoint）。
        2026-09-22 總督裁定 prune-latest 優先：預設只留最新一代；
        rollback 目標為已認證且相容之留存版本，無留存即 fail-closed。
        退役版本的中繼資料
        與 sha256 保留於 ``artifacts.weights.retired`` 作為證據；其檔案路徑
        自此不再受生命週期引用保護，可由 retention 掃除實體檔案。
        """
        weights = self.artifacts.get("weights")
        if not weights:
            return []
        versions = list(weights.get("versions", []))
        keep = max(1, int(keep_latest))
        keep_versions = {
            int(entry["version"]) for entry in versions[-keep:]
        }
        if self.active_weights_version:
            keep_versions.add(int(self.active_weights_version))
        extra = {str(p) for p in (extra_keep_paths or set())}
        retired_now: list[dict[str, Any]] = []
        remaining: list[dict[str, Any]] = []
        for entry in versions:
            protected = int(entry["version"]) in keep_versions or (
                str(entry.get("path", "")) in extra
            )
            if protected:
                remaining.append(entry)
                continue
            moved = dict(entry)
            moved["retired_at"] = _utcnow()
            retired_now.append(moved)
        if not retired_now:
            return []
        weights["versions"] = remaining
        retired_list = weights.setdefault("retired", [])
        retired_list.extend(retired_now)
        self.history.append(
            {
                "at": _utcnow(),
                "event": "weights_retired",
                "versions": [int(e["version"]) for e in retired_now],
                "kept": sorted(keep_versions),
            }
        )
        return retired_now

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": LIFECYCLE_FORMAT,
            "model_id": self.model_id,
            "state": self.state,
            "active_weights_version": self.active_weights_version,
            "artifacts": self.artifacts,
            "history": self.history,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ModelLifecycle":
        if data.get("format") != LIFECYCLE_FORMAT:
            raise ValueError("LIFECYCLE_FORMAT_UNSUPPORTED")
        lifecycle = cls(model_id=str(data["model_id"]))
        lifecycle.state = str(data.get("state") or "UNINITIALIZED")
        if lifecycle.state not in STATES:
            raise ValueError(f"LIFECYCLE_STATE_UNKNOWN:{lifecycle.state}")
        lifecycle.artifacts = dict(data.get("artifacts") or {})
        lifecycle.history = list(data.get("history") or [])
        lifecycle.active_weights_version = int(data.get("active_weights_version") or 0)
        return lifecycle

    # ── 持久化 ────────────────────────────────────────────────
    def save(self, directory: str | Path) -> Path:
        path = Path(directory) / "lifecycle.json"
        _atomic_write(
            path,
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
        )
        return path

    @classmethod
    def load(cls, directory: str | Path) -> "ModelLifecycle":
        path = Path(directory) / "lifecycle.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def load_or_create(cls, directory: str | Path, model_id: str) -> "ModelLifecycle":
        path = Path(directory) / "lifecycle.json"
        if path.is_file():
            return cls.load(directory)
        return cls(model_id=model_id)


__all__ = [
    "ARTIFACT_KINDS",
    "LIFECYCLE_FORMAT",
    "STATES",
    "ModelLifecycle",
]
