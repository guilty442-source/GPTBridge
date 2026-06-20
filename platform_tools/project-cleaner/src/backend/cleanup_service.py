from __future__ import annotations

import json
import os
import shutil
import stat as stat_module
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SECONDS_PER_DAY = 24 * 60 * 60
QUARANTINE_ROOT_NAME = ".GPTBridge_CleanerQuarantine"
DEFAULT_QUARANTINE_TTL_HOURS = 24
MAX_REPORTED_SKIPS = 200


class ProjectCleanupService:
    VERSION = "1.2.0"

    """Project-cleaner backend implementation.

    Cleanup rules live with the project-cleaner tool so the core stays thin.
    The service plans removable items first, then applies the plan by deleting
    or moving candidates into a short-lived quarantine directory.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()
        self.quarantine_root = self.project_root / QUARANTINE_ROOT_NAME

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _age_days(path: Path, now: float) -> float:
        try:
            return max(0.0, (now - path.stat(follow_symlinks=False).st_mtime) / SECONDS_PER_DAY)
        except OSError:
            return 0.0

    @staticmethod
    def _is_link_or_reparse_point(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            attrs = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
            reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            return bool(reparse_flag and attrs & reparse_flag)
        except OSError:
            return False

    @staticmethod
    def _directory_size_bytes(paths: list[Path]) -> int:
        total = 0
        for root in paths:
            if not root.exists():
                continue
            if ProjectCleanupService._is_link_or_reparse_point(root):
                continue
            if root.is_file():
                try:
                    total += root.stat(follow_symlinks=False).st_size
                except OSError:
                    pass
                continue
            for current, dirnames, filenames in os.walk(root, followlinks=False):
                current_path = Path(current)
                kept: list[str] = []
                for dirname in dirnames:
                    child = current_path / dirname
                    if ProjectCleanupService._is_link_or_reparse_point(child):
                        continue
                    kept.append(dirname)
                dirnames[:] = kept
                for filename in filenames:
                    path = current_path / filename
                    if ProjectCleanupService._is_link_or_reparse_point(path):
                        continue
                    try:
                        if path.is_file():
                            total += path.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        return total

    def _inside_project(self, path: Path) -> bool:
        try:
            self._safe_resolve(path).relative_to(self.project_root)
            return True
        except ValueError:
            return False

    def _protected_roots(self) -> list[Path]:
        runtime_root = self.project_root / "runtime"
        return [
            runtime_root / "profiles",
            runtime_root / "state",
            self.project_root / "browser-profile",
            self.project_root / "edge-profile",
            self.project_root / "backups",
            self.quarantine_root,
        ]

    def _is_protected(self, path: Path) -> bool:
        resolved = self._safe_resolve(path)
        for protected in self._protected_roots():
            try:
                resolved.relative_to(self._safe_resolve(protected))
                return True
            except ValueError:
                continue
        return False

    def _candidate_roots(self, requested_scope: str) -> tuple[str, list[Path] | None]:
        normalized_scope = (
            "global"
            if requested_scope in {"", "project", "global", "all", "full_project"}
            else requested_scope
        )
        runtime_root = self.project_root / "runtime"
        sandbox_root = self.project_root / ".GPTBridge_RuntimeSandbox"

        if normalized_scope == "sandbox":
            return normalized_scope, [sandbox_root]
        if normalized_scope == "runtime":
            return normalized_scope, [runtime_root]
        if normalized_scope == "global":
            return normalized_scope, [self.project_root]
        return normalized_scope, None

    @staticmethod
    def _append_skip(skipped: list[dict[str, Any]], item: dict[str, Any]) -> None:
        if len(skipped) < MAX_REPORTED_SKIPS:
            skipped.append(item)

    def _relative_path(self, path: Path) -> str:
        return self._safe_resolve(path).relative_to(self.project_root).as_posix()

    def _directory_reason(self, name: str) -> tuple[str, str] | None:
        lowered = name.lower()
        if lowered == "__pycache__":
            return "python bytecode cache", "low"
        if lowered in {".pytest_cache", ".mypy_cache", ".ruff_cache"}:
            return "tool cache directory", "low"
        if lowered == "cache":
            return "generated cache directory", "low"
        if lowered in {"temp", "tmp"}:
            return "temporary working directory", "low"
        return None

    def _file_reason(self, path: Path, now: float) -> tuple[str, str, float] | None:
        suffix = path.suffix.lower()
        name = path.name
        ttl_by_suffix = {
            ".tmp": 1.0,
            ".old": 14.0,
            ".bak": 30.0,
            ".log": 7.0,
        }
        if name == ".DS_Store":
            return "platform metadata file", "low", 0.0
        if suffix not in ttl_by_suffix:
            return None

        min_age_days = ttl_by_suffix[suffix]
        age_days = self._age_days(path, now)
        if age_days < min_age_days:
            return None
        if suffix == ".log":
            return f"log file older than {int(min_age_days)} days", "low", min_age_days
        if suffix == ".tmp":
            return f"temporary file older than {int(min_age_days)} day", "low", min_age_days
        return f"{suffix} file older than {int(min_age_days)} days", "medium", min_age_days

    @staticmethod
    def _summarize_items(items: list[dict[str, Any]]) -> dict[str, Any]:
        by_risk: dict[str, dict[str, Any]] = {}
        by_reason: dict[str, dict[str, Any]] = {}
        by_type: dict[str, dict[str, Any]] = {}

        for item in items:
            size = int(item.get("size_bytes") or 0)
            risk = str(item.get("risk") or "unknown")
            reason = str(item.get("reason") or "unknown")
            item_type = str(item.get("type") or "unknown")

            for bucket, key in (
                (by_risk, risk),
                (by_reason, reason),
                (by_type, item_type),
            ):
                current = bucket.setdefault(key, {"count": 0, "size_bytes": 0})
                current["count"] += 1
                current["size_bytes"] += size

        largest_items = sorted(
            items,
            key=lambda item: int(item.get("size_bytes") or 0),
            reverse=True,
        )[:10]
        return {
            "by_risk": by_risk,
            "by_reason": by_reason,
            "by_type": by_type,
            "largest_items": largest_items,
        }

    @staticmethod
    def _risk_count(items: list[dict[str, Any]], risk: str) -> int:
        return sum(1 for item in items if str(item.get("risk") or "") == risk)

    def _cleanup_health(
        self,
        scope: str,
        items: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        high_count = self._risk_count(items, "high")
        medium_count = self._risk_count(items, "medium")
        low_count = self._risk_count(items, "low")
        total_bytes = sum(int(item.get("size_bytes") or 0) for item in items)
        error_count = len(errors)
        skipped_count = len(skipped)
        score = 100
        score -= min(60, high_count * 25)
        score -= min(35, medium_count * 8)
        score -= min(30, error_count * 12)
        score -= min(10, skipped_count // 25)
        score = max(0, score)

        if not items and error_count == 0:
            state = "clean"
            safety_level = "safe"
            recommendation = "目前沒有可清理項目。"
            recommended_action = "none"
        elif error_count:
            state = "attention"
            safety_level = "review"
            recommendation = "清理前先處理錯誤或確認略過項目。"
            recommended_action = "review"
        elif high_count:
            state = "review"
            safety_level = "high"
            recommendation = "偵測到高風險項目，建議只用隔離清理。"
            recommended_action = "quarantine"
        elif medium_count:
            state = "review"
            safety_level = "medium"
            recommendation = "包含中風險項目，建議先隔離再確認。"
            recommended_action = "quarantine"
        else:
            state = "ready"
            safety_level = "low"
            recommendation = "低風險快取可清理，建議使用隔離清理保留還原能力。"
            recommended_action = "quarantine"

        return {
            "state": state,
            "safety_level": safety_level,
            "score": score,
            "item_count": len(items),
            "low_count": low_count,
            "medium_count": medium_count,
            "high_count": high_count,
            "error_count": error_count,
            "skipped_count": skipped_count,
            "total_bytes": total_bytes,
            "recommended_action": recommended_action,
            "recommendation": recommendation,
            "requires_review": bool(error_count or high_count or medium_count),
            "direct_delete_allowed": bool(items and not error_count and not high_count and not medium_count),
            "quarantine_ttl_hours": DEFAULT_QUARANTINE_TTL_HOURS,
            "scope": scope,
        }

    def _quarantine_health(self, batches: list[dict[str, Any]]) -> dict[str, Any]:
        total_bytes = sum(int(batch.get("size_bytes") or 0) for batch in batches)
        expired_count = sum(1 for batch in batches if batch.get("expired"))
        if not batches:
            state = "empty"
            recommendation = "隔離區目前沒有批次。"
        elif expired_count:
            state = "attention"
            recommendation = "有過期隔離批次，可以清理隔離區釋放空間。"
        else:
            state = "healthy"
            recommendation = "隔離區保留中，可在 TTL 到期後清理。"
        return {
            "state": state,
            "batch_count": len(batches),
            "expired_count": expired_count,
            "total_bytes": total_bytes,
            "ttl_hours": DEFAULT_QUARANTINE_TTL_HOURS,
            "recommendation": recommendation,
        }

    def _safe_candidate(self, path: Path, item_type: str) -> tuple[bool, str]:
        if not self._inside_project(path):
            return False, "outside project root"
        if self._is_protected(path):
            return False, "protected path"
        if self._is_link_or_reparse_point(path):
            return False, "link or reparse point"
        if not path.exists():
            return False, "path no longer exists"
        if item_type == "directory" and not path.is_dir():
            return False, "candidate is not a directory"
        if item_type == "file" and not path.is_file():
            return False, "candidate is not a file"
        return True, ""

    def plan_cleanup(self, scope: str, *, now: float | None = None) -> dict[str, Any]:
        requested_scope = str(scope or "global").strip().lower()
        normalized_scope, candidate_roots = self._candidate_roots(requested_scope)
        if candidate_roots is None:
            return {
                "ok": False,
                "scope": requested_scope,
                "requested_scope": requested_scope,
                "items": [],
                "skipped": [],
                "errors": [],
                "message": f"unsupported cleanup scope: {requested_scope}",
            }

        now = time.time() if now is None else now
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []

        excluded_dir_names = {
            ".git",
            ".venv",
            "node_modules",
            "browser-profile",
            "edge-profile",
            "backups",
            QUARANTINE_ROOT_NAME.lower(),
        }

        for raw_root in candidate_roots:
            root = self._safe_resolve(raw_root)
            if not root.exists():
                continue
            if not self._inside_project(root):
                self._append_skip(skipped, {"path": str(raw_root), "reason": "outside project root"})
                continue
            if self._is_protected(root):
                self._append_skip(skipped, {"path": self._relative_path(root), "reason": "protected root"})
                continue

            for current_raw, dirnames, filenames in os.walk(root, followlinks=False):
                current = Path(current_raw)
                if self._is_protected(current):
                    dirnames[:] = []
                    continue

                kept_dirnames: list[str] = []
                for dirname in dirnames:
                    child = current / dirname
                    lowered = dirname.lower()
                    if lowered in excluded_dir_names or self._is_protected(child):
                        continue
                    if self._is_link_or_reparse_point(child):
                        self._append_skip(
                            skipped,
                            {
                                "path": self._relative_path(child) if self._inside_project(child) else str(child),
                                "type": "directory",
                                "reason": "link or reparse point",
                            },
                        )
                        continue

                    directory_rule = self._directory_reason(dirname)
                    if directory_rule:
                        safe, reason = self._safe_candidate(child, "directory")
                        if not safe:
                            self._append_skip(
                                skipped,
                                {
                                    "path": self._relative_path(child) if self._inside_project(child) else str(child),
                                    "type": "directory",
                                    "reason": reason,
                                },
                            )
                            continue
                        rule_reason, risk = directory_rule
                        try:
                            size = self._directory_size_bytes([child])
                            items.append(
                                {
                                    "path": self._relative_path(child),
                                    "type": "directory",
                                    "size_bytes": size,
                                    "reason": rule_reason,
                                    "risk": risk,
                                    "age_days": round(self._age_days(child, now), 2),
                                    "min_age_days": 0,
                                }
                            )
                        except OSError as exc:
                            errors.append(
                                {
                                    "path": self._relative_path(child),
                                    "type": "directory",
                                    "message": str(exc),
                                }
                            )
                        continue
                    kept_dirnames.append(dirname)
                dirnames[:] = kept_dirnames

                for filename in filenames:
                    path = current / filename
                    if self._is_protected(path):
                        continue
                    if self._is_link_or_reparse_point(path):
                        self._append_skip(
                            skipped,
                            {
                                "path": self._relative_path(path) if self._inside_project(path) else str(path),
                                "type": "file",
                                "reason": "link or reparse point",
                            },
                        )
                        continue

                    file_rule = self._file_reason(path, now)
                    if not file_rule:
                        continue
                    safe, reason = self._safe_candidate(path, "file")
                    if not safe:
                        self._append_skip(
                            skipped,
                            {
                                "path": self._relative_path(path) if self._inside_project(path) else str(path),
                                "type": "file",
                                "reason": reason,
                            },
                        )
                        continue
                    rule_reason, risk, min_age_days = file_rule
                    try:
                        size = path.stat(follow_symlinks=False).st_size
                        items.append(
                            {
                                "path": self._relative_path(path),
                                "type": "file",
                                "size_bytes": size,
                                "reason": rule_reason,
                                "risk": risk,
                                "age_days": round(self._age_days(path, now), 2),
                                "min_age_days": min_age_days,
                            }
                        )
                    except OSError as exc:
                        errors.append(
                            {
                                "path": self._relative_path(path),
                                "type": "file",
                                "message": str(exc),
                            }
                        )

        total_bytes = sum(int(item.get("size_bytes", 0)) for item in items)
        file_count = sum(1 for item in items if item.get("type") == "file")
        dir_count = sum(1 for item in items if item.get("type") == "directory")
        health = self._cleanup_health(normalized_scope, items, skipped, errors)
        return {
            "ok": True,
            "scope": normalized_scope,
            "requested_scope": requested_scope,
            "items": items,
            "item_count": len(items),
            "file_count": file_count,
            "dir_count": dir_count,
            "total_bytes": total_bytes,
            "summary": self._summarize_items(items),
            "health": health,
            "skipped": skipped,
            "skipped_count": len(skipped),
            "errors": errors,
            "error_count": len(errors),
            "message": f"{normalized_scope} cleanup plan completed (items={len(items)})",
        }

    def _unique_quarantine_target(self, batch_dir: Path, rel_path: str) -> Path:
        target = batch_dir / rel_path
        if not target.exists():
            return target
        stem = target.stem
        suffix = target.suffix
        parent = target.parent
        for index in range(1, 1000):
            candidate = parent / f"{stem}.{index}{suffix}"
            if not candidate.exists():
                return candidate
        return parent / f"{stem}.{int(time.time())}{suffix}"

    def list_quarantine_batches(self) -> dict[str, Any]:
        batches: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []

        if not self.quarantine_root.exists():
            return {
                "ok": True,
                "batches": batches,
                "batch_count": 0,
                "total_bytes": 0,
                "health": self._quarantine_health(batches),
                "errors": errors,
                "message": "quarantine is empty",
            }

        for child in sorted(
            self.quarantine_root.iterdir(),
            key=lambda item: item.name,
            reverse=True,
        ):
            if self._is_link_or_reparse_point(child) or not child.is_dir():
                continue
            manifest_path = child / "manifest.json"
            item_count = 0
            size_bytes = self._directory_size_bytes([child])
            scope = ""
            created_at = ""
            if manifest_path.exists():
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    created_at = str(manifest.get("created_at") or "")
                    scope = str(manifest.get("scope") or "")
                    items = manifest.get("items", [])
                    if isinstance(items, list):
                        item_count = len(items)
                        manifest_size = sum(
                            int(item.get("size_bytes") or 0)
                            for item in items
                            if isinstance(item, dict)
                        )
                        if manifest_size > 0:
                            size_bytes = manifest_size
                except (OSError, json.JSONDecodeError) as exc:
                    errors.append({"path": child.name, "message": str(exc)})

            if not created_at:
                try:
                    created_at = datetime.fromtimestamp(
                        child.stat(follow_symlinks=False).st_mtime,
                        timezone.utc,
                    ).isoformat()
                except OSError:
                    created_at = ""
            age_hours = 0.0
            expires_at = ""
            expired = False
            try:
                modified_at = child.stat(follow_symlinks=False).st_mtime
                age_hours = max(0.0, (time.time() - modified_at) / 3600)
                expires_at = datetime.fromtimestamp(
                    modified_at + DEFAULT_QUARANTINE_TTL_HOURS * 3600,
                    timezone.utc,
                ).isoformat()
                expired = age_hours >= DEFAULT_QUARANTINE_TTL_HOURS
            except OSError:
                pass

            batches.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "created_at": created_at,
                    "scope": scope,
                    "item_count": item_count,
                    "size_bytes": size_bytes,
                    "age_hours": round(age_hours, 2),
                    "expires_at": expires_at,
                    "expired": expired,
                    "manifest_path": str(manifest_path) if manifest_path.exists() else "",
                }
            )

        health = self._quarantine_health(batches)
        return {
            "ok": len(errors) == 0,
            "batches": batches,
            "batch_count": len(batches),
            "total_bytes": sum(int(item.get("size_bytes") or 0) for item in batches),
            "health": health,
            "errors": errors,
            "message": f"quarantine batches listed (items={len(batches)})",
        }

    def purge_quarantine(self, older_than_hours: int = DEFAULT_QUARANTINE_TTL_HOURS) -> dict[str, Any]:
        if older_than_hours <= 0:
            older_than_hours = DEFAULT_QUARANTINE_TTL_HOURS
        purged_dirs = 0
        purged_bytes = 0
        errors: list[dict[str, str]] = []
        cutoff = time.time() - (older_than_hours * 60 * 60)

        if not self.quarantine_root.exists():
            return {
                "ok": True,
                "purged_dirs": purged_dirs,
                "purged_bytes": purged_bytes,
                "errors": errors,
                "message": "quarantine is empty",
            }

        for child in sorted(self.quarantine_root.iterdir(), key=lambda item: item.name):
            if self._is_link_or_reparse_point(child):
                errors.append({"path": str(child), "message": "skipped link or reparse point"})
                continue
            if not child.is_dir():
                continue
            try:
                if child.stat(follow_symlinks=False).st_mtime > cutoff:
                    continue
                size = self._directory_size_bytes([child])
                shutil.rmtree(child, ignore_errors=False)
                purged_dirs += 1
                purged_bytes += size
            except OSError as exc:
                errors.append({"path": str(child), "message": str(exc)})

        return {
            "ok": len(errors) == 0,
            "purged_dirs": purged_dirs,
            "purged_bytes": purged_bytes,
            "errors": errors,
            "message": f"quarantine purge completed (dirs={purged_dirs})",
        }

    def get_status(self) -> dict[str, Any]:
        quarantine = self.list_quarantine_batches()
        return {
            "ok": True,
            "version": self.VERSION,
            "project_root": str(self.project_root),
            "supported_scopes": ["global", "runtime", "sandbox"],
            "default_scope": "runtime",
            "quarantine_root": str(self.quarantine_root),
            "quarantine": quarantine,
            "quarantine_health": quarantine.get("health", {}),
            "message": "project cleaner status ready",
        }

    def cleanup_garbage(
        self,
        scope: str,
        dry_run: bool = False,
        *,
        quarantine: bool = False,
        quarantine_ttl_hours: int = DEFAULT_QUARANTINE_TTL_HOURS,
    ) -> dict[str, Any]:
        plan = self.plan_cleanup(scope)
        if not plan.get("ok"):
            return {
                **plan,
                "dry_run": dry_run,
                "cleaned_files": 0,
                "cleaned_dirs": 0,
                "cleaned_bytes": 0,
                "quarantine": quarantine,
            }

        items = list(plan.get("items", []))
        if dry_run:
            return {
                "ok": True,
                "scope": plan["scope"],
                "requested_scope": plan["requested_scope"],
                "dry_run": True,
                "quarantine": quarantine,
                "cleaned_files": plan["file_count"],
                "cleaned_dirs": plan["dir_count"],
                "cleaned_bytes": plan["total_bytes"],
                "planned_files": plan["file_count"],
                "planned_dirs": plan["dir_count"],
                "planned_bytes": plan["total_bytes"],
                "items": items,
                "summary": plan.get("summary", {}),
                "health": plan.get("health", {}),
                "skipped": plan.get("skipped", []),
                "skipped_count": plan.get("skipped_count", 0),
                "errors": plan.get("errors", []),
                "error_count": plan.get("error_count", 0),
                "message": f"{plan['scope']} cleanup dry-run completed (items={len(items)})",
            }

        purge_result = self.purge_quarantine(quarantine_ttl_hours) if quarantine else None
        batch_name = datetime.now().strftime("%Y%m%d_%H%M%S")
        batch_dir = self.quarantine_root / batch_name
        manifest_entries: list[dict[str, Any]] = []
        apply_errors = list(plan.get("errors", []))
        apply_skips = list(plan.get("skipped", []))
        cleaned_files = 0
        cleaned_dirs = 0
        cleaned_bytes = 0

        for item in items:
            rel_path = str(item.get("path", "")).strip()
            item_type = str(item.get("type", "")).strip()
            if not rel_path or item_type not in {"file", "directory"}:
                continue

            target = self.project_root / rel_path
            safe, reason = self._safe_candidate(target, item_type)
            if not safe:
                self._append_skip(
                    apply_skips,
                    {
                        "path": rel_path,
                        "type": item_type,
                        "reason": reason,
                    },
                )
                continue

            try:
                size = int(item.get("size_bytes", 0))
                if quarantine:
                    quarantine_target = self._unique_quarantine_target(batch_dir, rel_path)
                    quarantine_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(quarantine_target))
                    manifest_entries.append(
                        {
                            "original_path": rel_path,
                            "quarantine_path": quarantine_target.relative_to(batch_dir).as_posix(),
                            "type": item_type,
                            "size_bytes": size,
                            "reason": item.get("reason", ""),
                            "risk": item.get("risk", ""),
                        }
                    )
                elif item_type == "directory":
                    shutil.rmtree(target, ignore_errors=False)
                else:
                    target.unlink(missing_ok=True)

                if item_type == "directory":
                    cleaned_dirs += 1
                else:
                    cleaned_files += 1
                cleaned_bytes += size
            except OSError as exc:
                apply_errors.append(
                    {
                        "path": rel_path,
                        "type": item_type,
                        "message": str(exc),
                    }
                )

        manifest_path = None
        if quarantine and manifest_entries:
            batch_dir.mkdir(parents=True, exist_ok=True)
            manifest_path = batch_dir / "manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "created_at": self._iso_now(),
                        "project_root": str(self.project_root),
                        "scope": plan["scope"],
                        "items": manifest_entries,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
                newline="\n",
            )

        action_label = "quarantined" if quarantine else "deleted"
        return {
            "ok": len(apply_errors) == 0,
            "scope": plan["scope"],
            "requested_scope": plan["requested_scope"],
            "dry_run": False,
            "quarantine": quarantine,
            "quarantine_path": str(batch_dir) if quarantine and manifest_entries else "",
            "quarantine_manifest": str(manifest_path) if manifest_path else "",
            "quarantine_purge": purge_result,
            "cleaned_files": cleaned_files,
            "cleaned_dirs": cleaned_dirs,
            "cleaned_bytes": cleaned_bytes,
            "planned_files": plan["file_count"],
            "planned_dirs": plan["dir_count"],
            "planned_bytes": plan["total_bytes"],
            "items": items,
            "summary": plan.get("summary", {}),
            "health": self._cleanup_health(plan["scope"], items, apply_skips, apply_errors),
            "skipped": apply_skips,
            "skipped_count": len(apply_skips),
            "errors": apply_errors,
            "error_count": len(apply_errors),
            "message": (
                f"{plan['scope']} cleanup {action_label} items "
                f"(files={cleaned_files}, dirs={cleaned_dirs})"
            ),
        }

    def restore_quarantine(self, batch_name: str) -> dict[str, Any]:
        clean_name = Path(str(batch_name).strip()).name
        if not clean_name:
            return {"ok": False, "message": "quarantine batch is required"}

        batch_dir = (self.quarantine_root / clean_name).resolve()
        try:
            batch_dir.relative_to(self.quarantine_root.resolve())
        except ValueError:
            return {"ok": False, "message": "invalid quarantine batch"}

        manifest_path = batch_dir / "manifest.json"
        if not manifest_path.exists():
            return {"ok": False, "message": "quarantine manifest not found"}

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "message": f"invalid quarantine manifest: {exc}"}

        restored = 0
        skipped: list[dict[str, str]] = []
        errors: list[dict[str, str]] = []
        items = manifest.get("items", [])
        if not isinstance(items, list):
            return {"ok": False, "message": "invalid quarantine manifest items"}

        for item in items:
            if not isinstance(item, dict):
                continue
            original_rel = str(item.get("original_path", "")).strip()
            quarantine_rel = str(item.get("quarantine_path", "")).strip()
            if not original_rel or not quarantine_rel:
                continue
            source = (batch_dir / quarantine_rel).resolve()
            destination = (self.project_root / original_rel).resolve()
            try:
                source.relative_to(batch_dir)
                destination.relative_to(self.project_root)
            except ValueError:
                skipped.append({"path": original_rel, "reason": "invalid restore path"})
                continue
            if not source.exists():
                skipped.append({"path": original_rel, "reason": "quarantine item missing"})
                continue
            if destination.exists():
                skipped.append({"path": original_rel, "reason": "destination already exists"})
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                restored += 1
            except OSError as exc:
                errors.append({"path": original_rel, "message": str(exc)})

        return {
            "ok": len(errors) == 0,
            "restored": restored,
            "skipped": skipped,
            "errors": errors,
            "message": f"quarantine restore completed (items={restored})",
        }
