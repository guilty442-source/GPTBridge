# Project-cleaner scanner CleanupScannerAnalysisMixin.
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


class CleanupScannerAnalysisMixin:

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
            duplicate_min, large_min, large_age = self._analysis_thresholds()
            size_groups, large_files, file_count, total_bytes = (
                self._scan_analysis_entries(
                    normalized, duplicate_min, large_min, large_age
                )
            )
            quick_candidates = self._quick_group_candidates(
                size_groups, normalized
            )
            duplicate_groups = self._full_hash_duplicates(
                quick_candidates, normalized
            )

            duplicate_groups.sort(
                key=lambda item: int(item["wasted_bytes"]),
                reverse=True,
            )
            large_files.sort(
                key=lambda item: int(item["size_bytes"]),
                reverse=True,
            )
            result = self._analysis_result_document(
                normalized, file_count, total_bytes, duplicate_groups, large_files
            )
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
            duplicate_min, large_min, large_age = self._analysis_thresholds()
            size_groups, large_files, file_count, total_bytes = (
                self._legacy_scan_files(normalized, duplicate_min, large_min, large_age)
            )
            duplicate_groups = self._legacy_duplicate_groups(size_groups, normalized)
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

        def _analysis_thresholds(self) -> tuple[int, int, float]:
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
            return duplicate_min, large_min, large_age

        def _scan_analysis_entries(
            self,
            normalized: str,
            duplicate_min: int,
            large_min: int,
            large_age: float,
        ) -> tuple[dict[int, list[Path]], list[dict[str, Any]], int, int]:
            now = time.time()
            size_groups: dict[int, list[Path]] = {}
            large_files: list[dict[str, Any]] = []
            file_count = 0
            total_bytes = 0
            last_scan_progress = time.monotonic()
            self._emit_progress(
                "analyze", 2, "分析整個專案儲存空間", scope=normalized
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
                    self._scan_progress_emit(normalized, file_count, total_bytes, path)
                    last_scan_progress = progress_now
            self._emit_progress(
                "analyze-scan",
                60,
                f"檔案清冊完成，共 {file_count:,} 個檔案",
                scope=normalized,
                scanned_files=file_count,
                scanned_bytes=total_bytes,
            )
            return size_groups, large_files, file_count, total_bytes

        def _quick_group_candidates(
            self,
            size_groups: dict[int, list[Path]],
            normalized: str,
        ) -> list[tuple[int, list[Path]]]:
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
                    60 + int((group_index + 1) / max(1, len(candidates)) * 15),
                    f"快速比對 {group_index + 1:,}/{len(candidates):,} 組",
                    candidate_groups=len(candidates),
                )
            return quick_candidates

        def _full_hash_duplicates(
            self,
            quick_candidates: list[tuple[int, list[Path]]],
            normalized: str,
        ) -> list[dict[str, Any]]:
            duplicate_groups: list[dict[str, Any]] = []
            full_hash_total = sum(len(paths) for _size, paths in quick_candidates)
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
                            + int(full_hash_completed / max(1, full_hash_total) * 20),
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
                            "wasted_bytes": size * (len(duplicate_paths) - 1),
                            "paths": [
                                self._relative_path(path) for path in duplicate_paths
                            ],
                        }
                    )
            return duplicate_groups

        def _legacy_scan_files(
            self,
            normalized: str,
            duplicate_min: int,
            large_min: int,
            large_age: float,
        ) -> tuple[dict[int, list[Path]], list[dict[str, Any]], int, int]:
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
            return size_groups, large_files, file_count, total_bytes

        def _legacy_duplicate_groups(
            self,
            size_groups: dict[int, list[Path]],
            normalized: str,
        ) -> list[dict[str, Any]]:
            duplicate_groups: list[dict[str, Any]] = []
            candidates = [
                (size, paths)
                for size, paths in size_groups.items()
                if len(paths) > 1
            ]
            for group_index, (size, paths) in enumerate(candidates):
                quick_groups: dict[str, list[Path]] = {}
                for path in paths:
                    try:
                        quick_groups.setdefault(
                            self._quick_file_hash(path, size), []
                        ).append(path)
                    except OSError:
                        continue
                for quick_paths in quick_groups.values():
                    if len(quick_paths) < 2:
                        continue
                    duplicate_groups.extend(
                        self._legacy_quick_group_dups(quick_paths, size)
                    )
                self._emit_progress(
                    "analyze",
                    60 + int((group_index + 1) / max(1, len(candidates)) * 35),
                    "比對重複檔案",
                    duplicate_groups=len(duplicate_groups),
                )
            return duplicate_groups

        def _scan_progress_emit(
            self, normalized: str, file_count: int, total_bytes: int, path: Path
        ) -> None:
            scan_percent = min(55, 5 + int(10 * math.log10(max(1, file_count))))
            self._emit_progress(
                "analyze-scan",
                scan_percent,
                f"已掃描 {file_count:,} 個檔案",
                scope=normalized,
                scanned_files=file_count,
                scanned_bytes=total_bytes,
                current_path=self._relative_path(path),
            )

        def _legacy_quick_group_dups(
            self, quick_paths: list[Path], size: int
        ) -> list[dict[str, Any]]:
            full_groups: dict[str, list[Path]] = {}
            for path in quick_paths:
                try:
                    full_groups.setdefault(self._file_sha256(path), []).append(path)
                except OSError:
                    continue
            duplicate_groups: list[dict[str, Any]] = []
            for digest, duplicate_paths in full_groups.items():
                if len(duplicate_paths) < 2:
                    continue
                duplicate_groups.append(
                    {
                        "sha256": digest,
                        "size_bytes": size,
                        "copies": len(duplicate_paths),
                        "wasted_bytes": size * (len(duplicate_paths) - 1),
                        "paths": [
                            self._relative_path(path) for path in duplicate_paths
                        ],
                    }
                )
            return duplicate_groups

        @staticmethod
        def _analysis_result_document(
            normalized: str,
            file_count: int,
            total_bytes: int,
            duplicate_groups: list[dict[str, Any]],
            large_files: list[dict[str, Any]],
        ) -> dict[str, Any]:
            return {
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
