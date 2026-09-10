# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import stat as stat_module
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request

from .business_history import BusinessHistoryStore

try:
    from governance_rule.execution.git_tiers import audit_log, enforce
except Exception:
    audit_log = None
    enforce = None

from .cleanup_helpers import SECONDS_PER_DAY, LEGACY_QUARANTINE_ROOT_NAME, LEGACY_RECOVERY_ROOT_NAME, QUARANTINE_RELATIVE_PATH, RECOVERY_RELATIVE_PATH, CLEANER_RUNTIME_RELATIVE_PATH, DEFAULT_QUARANTINE_TTL_HOURS, DEFAULT_PLAN_TTL_MINUTES, MAX_REPORTED_SKIPS, MAX_HISTORY_RECORDS, PROGRESS_JSON_PREFIX, MANAGED_BACKUP_SCHEMA_VERSION, MANAGED_BACKUP_RETENTION_PER_OWNER, MANAGED_BACKUP_RELATIVE_ROOT, BACKUP_EXTRACT_RELATIVE_ROOT, SYSTEM_RESCUE_REQUIRED_PATHS, SYSTEM_RESCUE_REPAIR_ANOMALIES, PLAN_SCHEMA_VERSION, QUARANTINE_SCHEMA_VERSION, CORE_EXCLUDED_DIRECTORY_NAMES, CORE_PROTECTED_RELATIVE_PATHS, SOURCE_LIKE_SUFFIXES, _PROCESS_LOCKS_GUARD, _PROCESS_LOCKS, FALLBACK_RULES, ProgressCallback, _background_subprocess_kwargs, _deep_merge, _parse_iso


class CleanupScannerMixin:
    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path.absolute()


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


    def _inside_project(self, path: Path) -> bool:
        try:
            self._safe_resolve(path).relative_to(self.project_root)
            return True
        except ValueError:
            return False


    def _protected_roots(self) -> list[Path]:
        configured = self.rules.get("protected_relative_paths", [])
        roots = [
            self.project_root / str(item)
            for item in configured
            if str(item or "").strip()
        ]
        roots.extend([self.runtime_root, self.quarantine_root])
        unique: dict[str, Path] = {}
        for root in roots:
            unique[str(self._safe_resolve(root)).casefold()] = root
        return list(unique.values())


    def _is_protected(self, path: Path) -> bool:
        resolved = self._safe_resolve(path)
        try:
            relative_parts = resolved.relative_to(self.project_root).parts
        except ValueError:
            return True
        if len(relative_parts) >= 3 and relative_parts[0].casefold() == "platform_tools":
            tool_area = relative_parts[2].casefold()
            if tool_area == "dist":
                return True
            if (
                tool_area == "build"
                and len(relative_parts) >= 4
                and (
                    relative_parts[3].startswith("package-")
                    or relative_parts[3].startswith(".package-")
                )
            ):
                return True
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
        if normalized_scope == "sandbox":
            return normalized_scope, [self.project_root / ".GPTBridge_RuntimeSandbox"]
        if normalized_scope == "runtime":
            return normalized_scope, [self.project_root / "runtime"]
        if normalized_scope == "global":
            return normalized_scope, [self.project_root]
        return normalized_scope, None


    @staticmethod
    def _append_skip(skipped: list[dict[str, Any]], item: dict[str, Any]) -> None:
        if len(skipped) < MAX_REPORTED_SKIPS:
            skipped.append(item)


    def _relative_path(self, path: Path) -> str:
        return self._safe_resolve(path).relative_to(self.project_root).as_posix()


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
        safety_score = max(
            0,
            100
            - min(60, high_count * 25)
            - min(35, medium_count * 8)
            - min(30, error_count * 12)
            - min(10, skipped_count // 25),
        )
        size_mib = total_bytes / (1024 * 1024)
        calculated_penalty = int(math.log2(size_mib + 1) * 12) + len(items) // 3
        cleanliness_penalty = min(80, max(1 if items else 0, calculated_penalty))
        cleanliness_score = max(0, 100 - cleanliness_penalty)
        confidence_score = max(0, 100 - error_count * 20 - min(40, skipped_count // 5))
        if not items and not errors:
            state = "clean"
            recommendation = "目前沒有可清理項目。"
            recommended_action = "none"
        elif errors:
            state = "attention"
            recommendation = "清理前先處理掃描錯誤。"
            recommended_action = "review"
        elif high_count or medium_count:
            state = "review"
            recommendation = "包含需確認項目，只允許隔離清理。"
            recommended_action = "quarantine"
        else:
            state = "ready"
            recommendation = "低風險項目可套用已驗證計畫，仍建議先隔離。"
            recommended_action = "quarantine"
        return {
            "state": state,
            "safety_level": "high" if high_count else "medium" if medium_count else "low",
            "score": safety_score,
            "safety_score": safety_score,
            "cleanliness_score": cleanliness_score,
            "confidence_score": confidence_score,
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
            "quarantine_ttl_hours": self._quarantine_ttl_hours(),
            "scope": scope,
        }


    @contextmanager
    def _open_shared_read(self, source: Path) -> Iterator[BinaryIO]:
        if os.name != "nt":
            with source.open("rb") as source_file:
                yield source_file
            return
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = create_file(
            str(source),
            0x80000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x00000080 | 0x08000000,
            None,
        )
        invalid = wintypes.HANDLE(-1).value
        if handle in (None, invalid):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(source))
        try:
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
        except OSError:
            close_handle(handle)
            raise
        with os.fdopen(descriptor, "rb") as source_file:
            yield source_file


    def _file_sha256(self, path: Path) -> str:
        chunk_size = max(64 * 1024, int(self.rules.get("analysis", {}).get("hash_chunk_bytes") or 1024 * 1024))
        digest = hashlib.sha256()
        with self._open_shared_read(path) as source:
            while chunk := source.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()


    def _content_digest(self, path: Path, item_type: str) -> str:
        if item_type == "file":
            return self._file_sha256(path)
        digest = hashlib.sha256()
        for current, dirnames, filenames in os.walk(path, followlinks=False):
            current_path = Path(current)
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if not self._is_link_or_reparse_point(current_path / name)
            ]
            for filename in sorted(filenames):
                child = current_path / filename
                if self._is_link_or_reparse_point(child):
                    continue
                rel = child.relative_to(path).as_posix()
                digest.update(rel.encode("utf-8"))
                digest.update(self._file_sha256(child).encode("ascii"))
        return digest.hexdigest()


    def _regular_files_below(self, path: Path) -> list[Path]:
        files: list[Path] = []
        for current, dirnames, filenames in os.walk(path, followlinks=False):
            current_path = Path(current)
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if not self._is_link_or_reparse_point(current_path / name)
            ]
            for filename in sorted(filenames):
                child = current_path / filename
                if not self._is_link_or_reparse_point(child) and child.is_file():
                    files.append(child)
        return files


    def _is_locked(self, path: Path) -> bool:
        if os.name != "nt":
            return False
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x00010000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 if path.is_dir() else 0x00000080,
            None,
        )
        invalid = wintypes.HANDLE(-1).value
        if handle in (None, invalid):
            return ctypes.get_last_error() in {32, 33}
        kernel32.CloseHandle(handle)
        return False


    def _locked_descendant(self, path: Path) -> Path | None:
        if self._is_locked(path):
            return path
        if not path.is_dir():
            return None
        try:
            for current, dirnames, filenames in os.walk(path, followlinks=False):
                current_path = Path(current)
                safe_directories: list[str] = []
                for name in sorted(dirnames):
                    child = current_path / name
                    if self._is_link_or_reparse_point(child):
                        continue
                    if self._is_locked(child):
                        return child
                    safe_directories.append(name)
                dirnames[:] = safe_directories
                for name in sorted(filenames):
                    child = current_path / name
                    if self._is_link_or_reparse_point(child) or self._is_locked(child):
                        return child
        except OSError:
            return path
        return None


    @staticmethod
    def _locking_processes(path: Path) -> list[dict[str, Any]]:
        if os.name != "nt":
            return []
        try:
            from ctypes import wintypes

            class RM_UNIQUE_PROCESS(ctypes.Structure):
                _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]

            class RM_PROCESS_INFO(ctypes.Structure):
                _fields_ = [
                    ("Process", RM_UNIQUE_PROCESS),
                    ("strAppName", wintypes.WCHAR * 256),
                    ("strServiceShortName", wintypes.WCHAR * 64),
                    ("ApplicationType", wintypes.UINT),
                    ("AppStatus", wintypes.ULONG),
                    ("TSSessionId", wintypes.DWORD),
                    ("bRestartable", wintypes.BOOL),
                ]

            restart_manager = ctypes.WinDLL("Rstrtmgr")
            session = wintypes.DWORD()
            key = ctypes.create_unicode_buffer(33)
            if restart_manager.RmStartSession(ctypes.byref(session), 0, key) != 0:
                return []
            try:
                resources = (wintypes.LPCWSTR * 1)(str(path))
                if restart_manager.RmRegisterResources(session, 1, resources, 0, None, 0, None) != 0:
                    return []
                needed = wintypes.UINT(0)
                count = wintypes.UINT(0)
                reason = wintypes.DWORD(0)
                result = restart_manager.RmGetList(
                    session,
                    ctypes.byref(needed),
                    ctypes.byref(count),
                    None,
                    ctypes.byref(reason),
                )
                if result != 234 or needed.value == 0:
                    return []
                entries = (RM_PROCESS_INFO * needed.value)()
                count = wintypes.UINT(needed.value)
                if restart_manager.RmGetList(
                    session,
                    ctypes.byref(needed),
                    ctypes.byref(count),
                    entries,
                    ctypes.byref(reason),
                ) != 0:
                    return []
                return [
                    {
                        "pid": int(entries[index].Process.dwProcessId),
                        "name": str(entries[index].strAppName),
                        "restartable": bool(entries[index].bRestartable),
                    }
                    for index in range(count.value)
                ]
            finally:
                restart_manager.RmEndSession(session)
        except Exception:
            return []


    def _iter_analysis_entries(
        self,
        scope: str,
    ) -> Iterator[tuple[Path, os.stat_result]]:
        """Walk every in-boundary file once without cleanup exclusions."""

        _normalized, roots = self._candidate_roots(scope)
        if roots is None:
            return

        pending = list(reversed(roots))
        reparse_flag = int(
            getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        )
        while pending:
            current_path = pending.pop()
            try:
                resolved = current_path.resolve(strict=True)
                resolved.relative_to(self.project_root)
            except (OSError, ValueError):
                continue

            try:
                with os.scandir(resolved) as scanner:
                    entries = sorted(scanner, key=lambda item: item.name.casefold())
            except OSError:
                continue

            child_directories: list[Path] = []
            for entry in entries:
                try:
                    info = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                attributes = int(getattr(info, "st_file_attributes", 0))
                if entry.is_symlink() or (
                    reparse_flag and attributes & reparse_flag
                ):
                    # Reparse points may escape the governed project boundary.
                    continue
                path = Path(entry.path)
                if stat_module.S_ISDIR(info.st_mode):
                    child_directories.append(path)
                elif stat_module.S_ISREG(info.st_mode):
                    yield path, info

            pending.extend(reversed(child_directories))


    def _iter_analysis_files(self, scope: str) -> Iterator[Path]:
        """Compatibility iterator for callers that only need file paths."""

        for path, _info in self._iter_analysis_entries(scope):
            yield path


    def _quick_file_hash(self, path: Path, size: int) -> str:
        digest = hashlib.sha256()
        with self._open_shared_read(path) as source:
            digest.update(source.read(64 * 1024))
            if size > 128 * 1024:
                source.seek(max(0, size - 64 * 1024))
                digest.update(source.read(64 * 1024))
        return digest.hexdigest()


    def analyze_storage(self, scope: str = "global") -> dict[str, Any]:
        normalized, roots = self._candidate_roots(
            str(scope or "global").strip().lower()
        )
        if roots is None:
            return {
                "ok": False,
                "message": f"unsupported cleanup scope: {scope}",
            }
        analysis = (
            self.rules.get("analysis", {})
            if isinstance(self.rules.get("analysis"), dict)
            else {}
        )
        duplicate_min = max(
            1,
            int(analysis.get("duplicate_min_size_bytes") or 1024 * 1024),
        )
        large_min = max(
            1,
            int(analysis.get("large_file_min_size_bytes") or 10 * 1024 * 1024),
        )
        configured_large_age = analysis.get("large_file_min_age_days")
        large_age = max(
            0.0,
            float(14 if configured_large_age is None else configured_large_age),
        )
        now = time.time()
        size_groups: dict[int, list[Path]] = {}
        large_files: list[dict[str, Any]] = []
        file_count = 0
        total_bytes = 0
        last_scan_progress = time.monotonic()
        self._emit_progress(
            "analyze",
            2,
            "分析整個專案儲存空間",
            scope=normalized,
        )

        for path, info in self._iter_analysis_entries(normalized):
            file_count += 1
            total_bytes += info.st_size
            if info.st_size >= duplicate_min:
                size_groups.setdefault(int(info.st_size), []).append(path)
            age_days = max(0.0, (now - info.st_mtime) / SECONDS_PER_DAY)
            if info.st_size >= large_min and age_days >= large_age:
                large_files.append(
                    {
                        "path": self._relative_path(path),
                        "size_bytes": int(info.st_size),
                        "age_days": round(age_days, 2),
                    }
                )
            progress_now = time.monotonic()
            if (
                file_count % 250 == 0
                or progress_now - last_scan_progress >= 0.75
            ):
                scan_percent = min(
                    55,
                    5 + int(10 * math.log10(max(1, file_count))),
                )
                self._emit_progress(
                    "analyze-scan",
                    scan_percent,
                    f"已掃描 {file_count:,} 個檔案",
                    scope=normalized,
                    scanned_files=file_count,
                    scanned_bytes=total_bytes,
                    current_path=self._relative_path(path),
                )
                last_scan_progress = progress_now

        self._emit_progress(
            "analyze-scan",
            60,
            f"檔案清冊完成，共 {file_count:,} 個檔案",
            scope=normalized,
            scanned_files=file_count,
            scanned_bytes=total_bytes,
        )

        duplicate_groups: list[dict[str, Any]] = []
        candidates = [
            (size, paths)
            for size, paths in size_groups.items()
            if len(paths) > 1
        ]
        quick_candidates: list[tuple[int, list[Path]]] = []
        for group_index, (size, paths) in enumerate(candidates):
            quick_groups: dict[str, list[Path]] = {}
            for path in paths:
                try:
                    digest = self._quick_file_hash(path, size)
                    quick_groups.setdefault(digest, []).append(path)
                except OSError:
                    continue
            quick_candidates.extend(
                (size, quick_paths)
                for quick_paths in quick_groups.values()
                if len(quick_paths) >= 2
            )
            self._emit_progress(
                "analyze-quick-hash",
                60
                + int(
                    (group_index + 1) / max(1, len(candidates)) * 15
                ),
                f"快速比對 {group_index + 1:,}/{len(candidates):,} 組",
                candidate_groups=len(candidates),
            )

        full_hash_total = sum(
            len(paths) for _size, paths in quick_candidates
        )
        full_hash_completed = 0
        for size, quick_paths in quick_candidates:
            full_groups: dict[str, list[Path]] = {}
            for path in quick_paths:
                try:
                    digest = self._file_sha256(path)
                    full_groups.setdefault(digest, []).append(path)
                except OSError:
                    pass
                full_hash_completed += 1
                if (
                    full_hash_completed % 10 == 0
                    or full_hash_completed == full_hash_total
                ):
                    self._emit_progress(
                        "analyze-full-hash",
                        75
                        + int(
                            full_hash_completed
                            / max(1, full_hash_total)
                            * 20
                        ),
                        (
                            f"完整比對 {full_hash_completed:,}/"
                            f"{full_hash_total:,} 個候選檔案"
                        ),
                        current_path=self._relative_path(path),
                        hashed_files=full_hash_completed,
                        hash_total=full_hash_total,
                    )
            for digest, duplicate_paths in full_groups.items():
                if len(duplicate_paths) < 2:
                    continue
                duplicate_groups.append(
                    {
                        "sha256": digest,
                        "size_bytes": size,
                        "copies": len(duplicate_paths),
                        "wasted_bytes": size
                        * (len(duplicate_paths) - 1),
                        "paths": [
                            self._relative_path(path)
                            for path in duplicate_paths
                        ],
                    }
                )

        duplicate_groups.sort(
            key=lambda item: int(item["wasted_bytes"]),
            reverse=True,
        )
        large_files.sort(
            key=lambda item: int(item["size_bytes"]),
            reverse=True,
        )
        result = {
            "ok": True,
            "scope": normalized,
            "file_count": file_count,
            "total_bytes": total_bytes,
            "duplicate_groups": duplicate_groups,
            "duplicate_group_count": len(duplicate_groups),
            "duplicate_wasted_bytes": sum(
                int(item["wasted_bytes"]) for item in duplicate_groups
            ),
            "large_stale_files": large_files[:100],
            "large_stale_count": len(large_files),
            "message": (
                "storage analysis completed "
                f"(duplicates={len(duplicate_groups)})"
            ),
        }
        self._append_history(
            "analyze",
            ok=True,
            scope=normalized,
            item_count=file_count,
            duplicate_groups=len(duplicate_groups),
            duplicate_wasted_bytes=result["duplicate_wasted_bytes"],
        )
        self._emit_progress(
            "analyze",
            100,
            "整個專案儲存空間分析完成",
        )
        return result


    def _analyze_storage_legacy(self, scope: str = "global") -> dict[str, Any]:
        normalized, roots = self._candidate_roots(str(scope or "global").strip().lower())
        if roots is None:
            return {"ok": False, "message": f"unsupported cleanup scope: {scope}"}
        analysis = self.rules.get("analysis", {}) if isinstance(self.rules.get("analysis"), dict) else {}
        duplicate_min = max(1, int(analysis.get("duplicate_min_size_bytes") or 1024 * 1024))
        large_min = max(1, int(analysis.get("large_file_min_size_bytes") or 10 * 1024 * 1024))
        configured_large_age = analysis.get("large_file_min_age_days")
        large_age = max(0.0, float(14 if configured_large_age is None else configured_large_age))
        now = time.time()
        size_groups: dict[int, list[Path]] = {}
        large_files: list[dict[str, Any]] = []
        file_count = 0
        total_bytes = 0
        self._emit_progress("analyze", 2, "分析儲存空間", scope=normalized)
        for path in self._iter_analysis_files(normalized):
            try:
                info = path.stat(follow_symlinks=False)
            except OSError:
                continue
            file_count += 1
            total_bytes += info.st_size
            if info.st_size >= duplicate_min:
                size_groups.setdefault(int(info.st_size), []).append(path)
            age_days = max(0.0, (now - info.st_mtime) / SECONDS_PER_DAY)
            if info.st_size >= large_min and age_days >= large_age:
                large_files.append(
                    {
                        "path": self._relative_path(path),
                        "size_bytes": int(info.st_size),
                        "age_days": round(age_days, 2),
                    }
                )
        duplicate_groups: list[dict[str, Any]] = []
        candidates = [(size, paths) for size, paths in size_groups.items() if len(paths) > 1]
        for group_index, (size, paths) in enumerate(candidates):
            quick_groups: dict[str, list[Path]] = {}
            for path in paths:
                try:
                    quick_groups.setdefault(self._quick_file_hash(path, size), []).append(path)
                except OSError:
                    continue
            for quick_paths in quick_groups.values():
                if len(quick_paths) < 2:
                    continue
                full_groups: dict[str, list[Path]] = {}
                for path in quick_paths:
                    try:
                        full_groups.setdefault(self._file_sha256(path), []).append(path)
                    except OSError:
                        continue
                for digest, duplicate_paths in full_groups.items():
                    if len(duplicate_paths) < 2:
                        continue
                    duplicate_groups.append(
                        {
                            "sha256": digest,
                            "size_bytes": size,
                            "copies": len(duplicate_paths),
                            "wasted_bytes": size * (len(duplicate_paths) - 1),
                            "paths": [self._relative_path(path) for path in duplicate_paths],
                        }
                    )
            self._emit_progress(
                "analyze",
                60 + int((group_index + 1) / max(1, len(candidates)) * 35),
                "比對重複檔案",
                duplicate_groups=len(duplicate_groups),
            )
        duplicate_groups.sort(key=lambda item: int(item["wasted_bytes"]), reverse=True)
        large_files.sort(key=lambda item: int(item["size_bytes"]), reverse=True)
        result = {
            "ok": True,
            "scope": normalized,
            "file_count": file_count,
            "total_bytes": total_bytes,
            "duplicate_groups": duplicate_groups,
            "duplicate_group_count": len(duplicate_groups),
            "duplicate_wasted_bytes": sum(int(item["wasted_bytes"]) for item in duplicate_groups),
            "large_stale_files": large_files[:100],
            "large_stale_count": len(large_files),
            "message": f"storage analysis completed (duplicates={len(duplicate_groups)})",
        }
        self._append_history(
            "analyze",
            ok=True,
            scope=normalized,
            item_count=file_count,
            duplicate_groups=len(duplicate_groups),
            duplicate_wasted_bytes=result["duplicate_wasted_bytes"],
        )
        self._emit_progress("analyze", 100, "儲存空間分析完成")
        return result
