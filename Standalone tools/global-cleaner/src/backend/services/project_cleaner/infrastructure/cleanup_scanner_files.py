# Project-cleaner scanner CleanupScannerFilesMixin.
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

from .business_history import BusinessHistoryStore

try:
    from governance_rule.execution.git_tiers import audit_log, enforce
except Exception:
    audit_log = None
    enforce = None

from .cleanup_helpers import SECONDS_PER_DAY, LEGACY_QUARANTINE_ROOT_NAME, LEGACY_RECOVERY_ROOT_NAME, QUARANTINE_RELATIVE_PATH, RECOVERY_RELATIVE_PATH, CLEANER_RUNTIME_RELATIVE_PATH, DEFAULT_QUARANTINE_TTL_HOURS, DEFAULT_PLAN_TTL_MINUTES, MAX_REPORTED_SKIPS, MAX_HISTORY_RECORDS, PROGRESS_JSON_PREFIX, MANAGED_BACKUP_SCHEMA_VERSION, MANAGED_BACKUP_RETENTION_PER_OWNER, MANAGED_BACKUP_RELATIVE_ROOT, BACKUP_EXTRACT_RELATIVE_ROOT, SYSTEM_RESCUE_REQUIRED_PATHS, SYSTEM_RESCUE_REPAIR_ANOMALIES, PLAN_SCHEMA_VERSION, QUARANTINE_SCHEMA_VERSION, CORE_EXCLUDED_DIRECTORY_NAMES, CORE_PROTECTED_RELATIVE_PATHS, SOURCE_LIKE_SUFFIXES, _PROCESS_LOCKS_GUARD, _PROCESS_LOCKS, FALLBACK_RULES, ProgressCallback, _background_subprocess_kwargs, _deep_merge, _parse_iso


class CleanupScannerFilesMixin:

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
            safety_score, cleanliness_score, confidence_score = (
                self._cleanup_health_scores(
                    items, high_count, medium_count, error_count,
                    skipped_count, total_bytes,
                )
            )
            state, recommendation, recommended_action = self._cleanup_health_state(
                items, errors, high_count, medium_count
            )
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

        @staticmethod
        def _cleanup_health_scores(
            items: list[dict[str, Any]],
            high_count: int,
            medium_count: int,
            error_count: int,
            skipped_count: int,
            total_bytes: int,
        ) -> tuple[int, int, int]:
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
            return safety_score, cleanliness_score, confidence_score

        @staticmethod
        def _cleanup_health_state(
            items: list[dict[str, Any]],
            errors: list[dict[str, Any]],
            high_count: int,
            medium_count: int,
        ) -> tuple[str, str, str]:
            if not items and not errors:
                return "clean", "目前沒有可清理項目。", "none"
            if errors:
                return "attention", "清理前先處理掃描錯誤。", "review"
            if high_count or medium_count:
                return "review", "包含需確認項目，只允許隔離清理。", "quarantine"
            return "ready", "低風險項目可套用已驗證計畫，仍建議先隔離。", "quarantine"
