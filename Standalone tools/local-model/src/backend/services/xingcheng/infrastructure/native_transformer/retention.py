"""星澄資料保留策略（``star-retention-policy/v1``）。

限制本地模型 runtime 累積的資料量：舊訓練 job 目錄、日誌、
成熟度報告、self-learning 快照各自有保留上限。

保護規則（fail-closed，永遠不刪）：

- 任何被 ``lifecycle.json`` artefact 版本引用的路徑
  （weights / tokenizer / config / training_state / evaluation_report）；
- ``runtime/settings/native-engine.json`` 指向的 checkpoint；
- 不在本模組管轄清單內的任何路徑。

政策檔：``runtime/settings/retention.json``（``enabled=false`` 即關閉）。
刪除動作 append 到 ``xingcheng/runtime/logs/retention.jsonl``。
"""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

POLICY_FORMAT = "star-retention-policy/v1"
POLICY_RELATIVE = "runtime/settings/retention.json"
SETTINGS_RELATIVE = "runtime/settings/native-engine.json"
LOG_RELATIVE = "xingcheng/runtime/logs"
AUDIT_RELATIVE = "xingcheng/runtime/logs/retention.jsonl"
JOBS_RELATIVE = "xingcheng/runtime/models/jobs"
LIFECYCLE_GLOB = "xingcheng/runtime/models/lifecycle/*/lifecycle.json"
SNAPSHOT_RELATIVE = "xingcheng/runtime/state/self-learning"
EVIDENCE_GLOBS = (
    "xingcheng/runtime/logs/maturity-*.json",
    "xingcheng/runtime/logs/self-learning-*.json",
)
_EVIDENCE_PATH_KEYS = frozenset(
    {"checkpoint", "checkpoint_path", "weights", "weights_path", "artifact_path"}
)


@dataclass
class RetentionPolicy:
    """保留上限；``enabled=False`` 時任何動作都不執行。"""

    enabled: bool = True
    keep_job_dirs: int = 3            # 保留最新 N 個 governed job 目錄
    keep_logs_days: float = 30.0      # *.log / *.err.log 保留天數
    keep_report_days: float = 30.0    # maturity-*.json / self-learning-*.json
    keep_maturity_reports: int = 10   # 另加數量上限（取較嚴者）
    keep_self_learning_reports: int = 10
    keep_snapshots: int = 5           # self-learning 匯出快照數量

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["format"] = POLICY_FORMAT
        return data

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "RetentionPolicy":
        fields = {
            key: value
            for key, value in dict(data).items()
            if key in cls.__dataclass_fields__
        }
        return cls(**fields)


def policy_path(tool_root: str | Path) -> Path:
    return Path(tool_root) / POLICY_RELATIVE


def load_policy(tool_root: str | Path) -> RetentionPolicy:
    path = policy_path(tool_root)
    if not path.is_file():
        return RetentionPolicy()
    try:
        return RetentionPolicy.from_dict(
            json.loads(path.read_text(encoding="utf-8"))
        )
    except (OSError, json.JSONDecodeError, TypeError):
        return RetentionPolicy()


def _utcnow() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _protected_paths(tool_root: Path) -> set[Path]:
    """lifecycle artefact + runtime 設定指向的路徑——永遠不刪。"""
    protected: set[Path] = set()
    for lifecycle_file in tool_root.glob(LIFECYCLE_GLOB):
        try:
            data = json.loads(lifecycle_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        for group in (data.get("artifacts") or {}).values():
            for entry in group.get("versions", []):
                raw = str(entry.get("path") or "")
                if raw:
                    try:
                        protected.add(Path(raw).resolve())
                    except OSError:
                        pass
    settings_file = tool_root / SETTINGS_RELATIVE
    if settings_file.is_file():
        try:
            settings = json.loads(settings_file.read_text(encoding="utf-8"))
            pinned = str(settings.get("checkpoint") or "")
            if pinned:
                protected.add((tool_root / pinned).resolve())
        except (OSError, json.JSONDecodeError):
            pass

    # G65: maturity and self-learning evidence may reference a checkpoint
    # that is no longer present in a lifecycle generation.  Those references
    # are evidence roots and must survive retention as well.
    for pattern in EVIDENCE_GLOBS:
        for evidence_file in tool_root.glob(pattern):
            try:
                evidence = json.loads(evidence_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            stack: list[Any] = [evidence]
            while stack:
                value = stack.pop()
                if isinstance(value, dict):
                    for key, child in value.items():
                        if key in _EVIDENCE_PATH_KEYS and isinstance(child, str) and child.strip():
                            try:
                                candidate = Path(child)
                                protected.add(
                                    (candidate if candidate.is_absolute() else tool_root / candidate).resolve()
                                )
                            except OSError:
                                pass
                        elif isinstance(child, (dict, list)):
                            stack.append(child)
                elif isinstance(value, list):
                    stack.extend(value)
    return protected


def _is_protected(path: Path, protected: set[Path]) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        return True  # 無法解析 → fail-closed 當保護
    if resolved in protected:
        return True
    # 目錄含任一受保護檔案 → 整個目錄保留
    return any(
        resolved == parent or resolved in parent.parents
        for parent in (p.parent for p in protected)
    )


def _delete_path(path: Path, dry_run: bool) -> bool:
    if dry_run:
        return True
    try:
        if path.is_dir():
            for child in sorted(path.rglob("*"), reverse=True):
                if child.is_file() or child.is_symlink():
                    child.unlink()
                elif child.is_dir():
                    child.rmdir()
            path.rmdir()
        else:
            path.unlink()
        return True
    except OSError:
        return False


def _plan_job_dirs(tool_root: Path, policy: RetentionPolicy,
                   protected: set[Path]) -> list[Path]:
    jobs_dir = tool_root / JOBS_RELATIVE
    if not jobs_dir.is_dir():
        return []
    dirs = [p for p in jobs_dir.iterdir() if p.is_dir()]
    dirs.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    candidates = dirs[int(policy.keep_job_dirs):]
    return [d for d in candidates if not _is_protected(d, protected)]


def _plan_logs(tool_root: Path, policy: RetentionPolicy) -> list[Path]:
    logs_dir = tool_root / LOG_RELATIVE
    if not logs_dir.is_dir():
        return []
    now = time.time()
    cutoff = now - float(policy.keep_logs_days) * 86400.0
    report_cutoff = now - float(policy.keep_report_days) * 86400.0
    victims: list[Path] = []
    for path in logs_dir.iterdir():
        if not path.is_file() or path.name == "retention.jsonl":
            continue
        mtime = path.stat().st_mtime
        if path.suffix == ".log" and mtime < cutoff:
            victims.append(path)
        elif path.name.startswith(("maturity-", "self-learning-")) \
                and path.suffix == ".json" and mtime < report_cutoff:
            victims.append(path)
    # 數量上限（即使未過期）
    for prefix, keep in (
        ("maturity-", int(policy.keep_maturity_reports)),
        ("self-learning-", int(policy.keep_self_learning_reports)),
    ):
        reports = sorted(
            (p for p in logs_dir.glob(f"{prefix}*.json") if p.is_file()),
            key=lambda p: p.stat().st_mtime, reverse=True,
        )
        victims.extend(r for r in reports[keep:] if r not in victims)
    return victims


def _plan_snapshots(tool_root: Path, policy: RetentionPolicy) -> list[Path]:
    snap_dir = tool_root / SNAPSHOT_RELATIVE
    if not snap_dir.is_dir():
        return []
    snaps = sorted(
        (p for p in snap_dir.glob("*.jsonl") if p.is_file()),
        key=lambda p: p.stat().st_mtime, reverse=True,
    )
    return snaps[int(policy.keep_snapshots):]


def apply_retention(
    tool_root: str | Path,
    *,
    policy: RetentionPolicy | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """執行一輪保留清理；``dry_run=True`` 只回報不刪除。"""
    root = Path(tool_root).resolve()
    resolved_policy = policy or load_policy(root)
    if not resolved_policy.enabled:
        return {"ok": True, "action": "disabled", "checked_at": _utcnow()}

    protected = _protected_paths(root)
    plans = {
        "job_dirs": _plan_job_dirs(root, resolved_policy, protected),
        "logs": _plan_logs(root, resolved_policy),
        "snapshots": _plan_snapshots(root, resolved_policy),
    }
    deleted: list[dict[str, Any]] = []
    for category, paths in plans.items():
        for path in paths:
            if category == "logs" and _is_protected(path, protected):
                continue
            size = 0
            try:
                if path.is_dir():
                    size = sum(
                        f.stat().st_size for f in path.rglob("*")
                        if f.is_file()
                    )
                else:
                    size = path.stat().st_size
            except OSError:
                pass
            if _delete_path(path, dry_run):
                deleted.append({
                    "category": category,
                    "path": str(path),
                    "bytes": size,
                })

    result = {
        "ok": True,
        "action": "dry-run" if dry_run else "applied",
        "policy": resolved_policy.to_dict(),
        "protected_paths": len(protected),
        "deleted": deleted,
        "deleted_bytes": sum(item["bytes"] for item in deleted),
        "checked_at": _utcnow(),
    }
    if not dry_run:
        audit = root / AUDIT_RELATIVE
        audit.parent.mkdir(parents=True, exist_ok=True)
        with audit.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "at": result["checked_at"],
                "deleted": deleted,
                "deleted_bytes": result["deleted_bytes"],
            }, ensure_ascii=False) + "\n")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="星澄資料保留清理")
    parser.add_argument("--tool-root", required=True,
                        help="local-model 工具根目錄")
    parser.add_argument("--apply", action="store_true",
                        help="實際刪除（預設為 dry-run 只回報）")
    parser.add_argument("--status", action="store_true",
                        help="只顯示政策與受保護路徑數")
    args = parser.parse_args(argv)

    root = Path(args.tool_root).resolve()
    if args.status:
        policy = load_policy(root)
        print(json.dumps({
            "policy": policy.to_dict(),
            "protected_paths": len(_protected_paths(root)),
        }, ensure_ascii=False, indent=2))
        return 0

    result = apply_retention(root, dry_run=not args.apply)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "POLICY_FORMAT",
    "RetentionPolicy",
    "apply_retention",
    "load_policy",
    "policy_path",
]
