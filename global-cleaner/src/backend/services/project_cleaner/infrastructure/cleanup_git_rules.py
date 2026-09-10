# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .cleanup_constants import (
    SOURCE_LIKE_SUFFIXES,
    _background_subprocess_kwargs,
)

try:
    from governance_rule.execution.git_tiers import audit_log, enforce
except Exception:
    audit_log = None
    enforce = None


class GitRulesMixin:
    """Git snapshot, rule matching, candidate evaluation, and snapshots."""

    def _git_snapshot(self) -> tuple[set[str], dict[str, Any]]:
        if self._git_tracked_cache is not None and self._git_status_cache is not None:
            return self._git_tracked_cache, self._git_status_cache
        if not (self.project_root / ".git").exists():
            self._git_tracked_cache = set()
            self._git_status_cache = {"available": False, "repository": False, "error": ""}
            return self._git_tracked_cache, self._git_status_cache

        command = "git ls-files -z"
        actor = "global-cleaner"
        if enforce is not None:
            allowed, message = enforce(command, actor=actor)
            if not allowed:
                self._git_tracked_cache = set()
                self._git_status_cache = {
                    "available": False,
                    "repository": True,
                    "tracked_count": 0,
                    "error": message,
                    "audit": {"allowed": False, "message": message},
                }
                return self._git_tracked_cache, self._git_status_cache

        try:
            completed = subprocess.run(
                ["git", "-C", str(self.project_root), "ls-files", "-z"],
                capture_output=True,
                timeout=10,
                check=False,
                **_background_subprocess_kwargs(),
            )
            if completed.returncode != 0:
                raise OSError(completed.stderr.decode("utf-8", errors="replace").strip())
            tracked = {
                item.replace("\\", "/")
                for item in completed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
                if item
            }
            self._git_tracked_cache = tracked
            self._git_status_cache = {
                "available": True,
                "repository": True,
                "tracked_count": len(tracked),
                "error": "",
            }
            if audit_log is not None:
                audit_log(
                    1,
                    command,
                    actor,
                    True,
                    f"tier-1 direct execution; cwd={self.project_root}",
                    phase="execution",
                    result="success",
                    returncode=completed.returncode,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            self._git_tracked_cache = set()
            self._git_status_cache = {
                "available": False,
                "repository": True,
                "tracked_count": 0,
                "error": str(exc),
            }
            if audit_log is not None:
                audit_log(
                    1,
                    command,
                    actor,
                    True,
                    f"execution failed: {exc}; cwd={self.project_root}",
                    phase="execution",
                    result="failure",
                    returncode=None,
                )
        return self._git_tracked_cache, self._git_status_cache

    def _git_protection_reason(self, path: Path, item_type: str) -> str:
        tracked, status = self._git_snapshot()
        if status.get("repository") and not status.get("available"):
            return "git protection unavailable"
        rel_path = self._relative_path(path)
        if item_type == "file" and rel_path in tracked:
            return "git-tracked file"
        prefix = rel_path.rstrip("/") + "/"
        if item_type == "directory" and any(item.startswith(prefix) for item in tracked):
            return "directory contains git-tracked files"
        return ""

    def _directory_contains_user_content(self, path: Path) -> bool:
        try:
            for current, dirnames, filenames in os.walk(path, followlinks=False):
                current_path = Path(current)
                dirnames[:] = [
                    name
                    for name in dirnames
                    if not self._is_link_or_reparse_point(current_path / name)
                ]
                for filename in filenames:
                    child = current_path / filename
                    if self._is_link_or_reparse_point(child):
                        return True
                    if child.suffix.casefold() in SOURCE_LIKE_SUFFIXES:
                        return True
        except OSError:
            return True
        return False

    def _safe_candidate(
        self,
        path: Path,
        item_type: str,
        *,
        check_git: bool = True,
    ) -> tuple[bool, str]:
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
        if check_git:
            reason = self._git_protection_reason(path, item_type)
            if reason:
                return False, reason
        return True, ""

    def _directory_rule(
        self,
        name: str,
        *,
        path: Path | None = None,
        scope: str = "global",
    ) -> dict[str, Any] | None:
        lowered = name.casefold()
        relative = (
            self._relative_path(path).casefold()
            if path is not None and self._inside_project(path)
            else ""
        )
        for rule in self.rules.get("directory_rules", []):
            if not isinstance(rule, dict):
                continue
            if bool(rule.get("sandbox_only")) and scope != "sandbox":
                continue
            names = {str(item).casefold() for item in rule.get("names", [])}
            patterns = [str(item).casefold() for item in rule.get("patterns", [])]
            relative_patterns = [
                str(item).replace("\\", "/").casefold()
                for item in rule.get("relative_patterns", [])
            ]
            if lowered in names or any(
                fnmatch.fnmatch(lowered, pattern) for pattern in patterns
            ) or (
                relative
                and any(
                    fnmatch.fnmatch(relative, pattern)
                    for pattern in relative_patterns
                )
            ):
                return rule
        return None

    def _directory_reason(self, name: str) -> tuple[str, str] | None:
        rule = self._directory_rule(name)
        if rule is None:
            return None
        return str(rule.get("reason") or "generated directory"), str(rule.get("risk") or "low")

    def _file_rule(self, path: Path, now: float) -> dict[str, Any] | None:
        name = path.name
        relative = (
            self._relative_path(path).casefold()
            if self._inside_project(path)
            else ""
        )
        for rule in self.rules.get("file_rules", []):
            if not isinstance(rule, dict):
                continue
            patterns = [str(item) for item in rule.get("patterns", [])]
            relative_patterns = [
                str(item).replace("\\", "/").casefold()
                for item in rule.get("relative_patterns", [])
            ]
            name_matches = any(
                fnmatch.fnmatch(name.casefold(), pattern.casefold())
                for pattern in patterns
            )
            relative_matches = bool(relative) and any(
                fnmatch.fnmatch(relative, pattern)
                for pattern in relative_patterns
            )
            if not name_matches and not relative_matches:
                continue
            min_age_days = max(0.0, float(rule.get("min_age_days") or 0))
            if self._age_days(path, now) < min_age_days:
                continue
            return rule
        return None

    def _file_reason(self, path: Path, now: float) -> tuple[str, str, float] | None:
        rule = self._file_rule(path, now)
        if rule is None:
            return None
        return (
            str(rule.get("reason") or "generated file"),
            str(rule.get("risk") or "low"),
            max(0.0, float(rule.get("min_age_days") or 0)),
        )

    @staticmethod
    def _stat_identity(path: Path) -> dict[str, int]:
        info = path.stat(follow_symlinks=False)
        return {
            "device": int(getattr(info, "st_dev", 0)),
            "file_id": int(getattr(info, "st_ino", 0)),
            "mode": int(info.st_mode),
            "mtime_ns": int(info.st_mtime_ns),
            "size": int(info.st_size),
        }

    def _candidate_snapshot(self, path: Path, item_type: str) -> dict[str, Any]:
        digest = hashlib.sha256()
        root_identity = self._stat_identity(path)
        digest.update(json.dumps(root_identity, sort_keys=True).encode("utf-8"))
        total_bytes = root_identity["size"] if item_type == "file" else 0
        entry_count = 1
        file_count = 1 if item_type == "file" else 0
        if item_type == "directory":
            for current, dirnames, filenames in os.walk(path, followlinks=False):
                current_path = Path(current)
                kept_dirs: list[str] = []
                for dirname in sorted(dirnames):
                    child = current_path / dirname
                    if self._is_link_or_reparse_point(child):
                        continue
                    kept_dirs.append(dirname)
                    identity = self._stat_identity(child)
                    rel = child.relative_to(path).as_posix()
                    digest.update(f"D:{rel}:".encode("utf-8"))
                    digest.update(json.dumps(identity, sort_keys=True).encode("utf-8"))
                    entry_count += 1
                dirnames[:] = kept_dirs
                for filename in sorted(filenames):
                    child = current_path / filename
                    if self._is_link_or_reparse_point(child):
                        continue
                    identity = self._stat_identity(child)
                    rel = child.relative_to(path).as_posix()
                    digest.update(f"F:{rel}:".encode("utf-8"))
                    digest.update(json.dumps(identity, sort_keys=True).encode("utf-8"))
                    total_bytes += identity["size"]
                    entry_count += 1
                    file_count += 1
        return {
            "digest": digest.hexdigest(),
            "size_bytes": total_bytes,
            "entry_count": entry_count,
            "file_count": file_count,
            "root": root_identity,
        }

    @staticmethod
    def _summarize_items(items: list[dict[str, Any]]) -> dict[str, Any]:
        by_risk: dict[str, dict[str, Any]] = {}
        by_reason: dict[str, dict[str, Any]] = {}
        by_type: dict[str, dict[str, Any]] = {}
        for item in items:
            size = int(item.get("size_bytes") or 0)
            for bucket, key in (
                (by_risk, str(item.get("risk") or "unknown")),
                (by_reason, str(item.get("reason") or "unknown")),
                (by_type, str(item.get("type") or "unknown")),
            ):
                current = bucket.setdefault(key, {"count": 0, "size_bytes": 0})
                current["count"] += 1
                current["size_bytes"] += size
        return {
            "by_risk": by_risk,
            "by_reason": by_reason,
            "by_type": by_type,
            "largest_items": sorted(
                items,
                key=lambda item: int(item.get("size_bytes") or 0),
                reverse=True,
            )[:10],
        }

    @staticmethod
    def _risk_count(items: list[dict[str, Any]], risk: str) -> int:
        return sum(1 for item in items if str(item.get("risk") or "") == risk)
