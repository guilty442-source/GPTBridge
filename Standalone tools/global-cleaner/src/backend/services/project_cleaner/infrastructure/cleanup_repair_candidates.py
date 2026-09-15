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


class CleanupRepairCandidatesMixin:

    @staticmethod
    def _process_is_alive(process_id: int) -> bool:
        if process_id <= 0:
            return False
        if process_id == os.getpid():
            return True
        if os.name == "nt":
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(0x1000, False, process_id)
            if not process:
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(
                    process,
                    ctypes.byref(exit_code),
                ):
                    return True
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(process)
        try:
            os.kill(process_id, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    @staticmethod
    def _is_sha256_text(value: str) -> bool:
        normalized = str(value or "").strip()
        return len(normalized) == 64 and all(
            character in "0123456789abcdefABCDEF"
            for character in normalized
        )

    @staticmethod
    def _repair_diagnostic(
        diagnostics: list[dict[str, Any]],
        path_str: str,
        layer: str,
        anomaly: str,
        reason: str,
    ) -> None:
        diagnostics.append(
            {
                "path": path_str,
                "layer": layer,
                "anomaly": anomaly,
                "repairable": False,
                "reason": reason,
            }
        )

    def _add_repair_candidate(
        self,
        candidates: list[dict[str, Any]],
        diagnostics: list[dict[str, Any]],
        seen: set[str],
        path: Path,
        *,
        layer: str,
        anomaly: str,
        reason: str,
    ) -> None:
        absolute = Path(os.path.abspath(path))
        key = os.path.normcase(str(absolute))
        if key in seen or not absolute.exists():
            return
        seen.add(key)
        if not self._inside_project(absolute):
            self._repair_diagnostic(
                diagnostics, str(absolute), layer, anomaly,
                "path is outside project root",
            )
            return
        if self._is_link_or_reparse_point(absolute):
            self._repair_diagnostic(
                diagnostics, self._relative_path(absolute), layer, anomaly,
                "link or reparse point requires manual review",
            )
            return
        item_type = "directory" if absolute.is_dir() else "file"
        if item_type == "file" and not absolute.is_file():
            return
        try:
            snapshot = self._candidate_snapshot(absolute, item_type)
        except OSError as exc:
            self._repair_diagnostic(
                diagnostics, self._relative_path(absolute), layer, anomaly, str(exc)
            )
            return
        candidates.append(
            self._repair_candidate_item(
                absolute, item_type, snapshot, layer, anomaly, reason
            )
        )


    def _repair_anomaly_candidates(
        self,
        *,
        include_shared: bool,
        now: float | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Find only deterministic anomalies that have a recoverable repair."""


        current_time = time.time() if now is None else float(now)
        candidates: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        seen: set[str] = set()


        def add_candidate(
            path: Path,
            *,
            layer: str,
            anomaly: str,
            reason: str,
        ) -> None:
            self._add_repair_candidate(
                candidates, diagnostics, seen, path,
                layer=layer, anomaly=anomaly, reason=reason,
            )

        self._scan_plan_root_anomalies(add_candidate, current_time)
        if include_shared:
            self._scan_shared_signature_anomalies(add_candidate)
            self._scan_tool_build_anomalies(add_candidate, current_time)
        self._append_rule_warning_diagnostics(diagnostics)
        return candidates, diagnostics

    def _scan_plan_root_anomalies(
        self,
        add_candidate: Callable[..., None],
        current_time: float,
    ) -> None:
        if self.plan_root.is_dir() and not self._is_link_or_reparse_point(
            self.plan_root
        ):
            for path in sorted(self.plan_root.iterdir(), key=lambda item: item.name):
                if self._is_link_or_reparse_point(path) or not path.is_file():
                    continue
                if path.suffix.casefold() == ".tmp":
                    try:
                        age_seconds = current_time - path.stat(
                            follow_symlinks=False
                        ).st_mtime
                    except OSError:
                        continue
                    if age_seconds >= 10 * 60:
                        add_candidate(
                            path,
                            layer="cleaner",
                            anomaly="orphan-atomic-temp",
                            reason="stale atomic-write temporary file",
                        )
                    continue
                if path.suffix.casefold() != ".json":
                    continue
                invalid_reason = self._preview_plan_invalid_reason(path)
                if invalid_reason:
                    add_candidate(
                        path,
                        layer="cleaner",
                        anomaly="stale-preview-plan",
                        reason=invalid_reason,
                    )


    def _scan_shared_signature_anomalies(
        self, add_candidate: Callable[..., None],
    ) -> None:
        build_stamp = (
            self.project_root
            / "launcher"
            / "state"
            / "production-build.sha256"
        )
        if build_stamp.is_file() and not self._is_link_or_reparse_point(
            build_stamp
        ):
            try:
                stamp_value = build_stamp.read_text(encoding="ascii")
            except (OSError, UnicodeError):
                stamp_value = ""
            if not self._is_sha256_text(stamp_value):
                add_candidate(
                    build_stamp,
                    layer="shared",
                    anomaly="invalid-production-signature",
                    reason=(
                        "invalid production signature; launcher will rebuild "
                        "instead of mixing generations"
                    ),
                )


    def _scan_tool_build_anomalies(
        self,
        add_candidate: Callable[..., None],
        current_time: float,
    ) -> None:
        tools_root = self.project_root / "platform_tools"
        if tools_root.is_dir() and not self._is_link_or_reparse_point(
            tools_root
        ):
            for tool_dir in sorted(
                tools_root.iterdir(),
                key=lambda item: item.name,
            ):
                build_root = tool_dir / "build"
                if (
                    not tool_dir.is_dir()
                    or self._is_link_or_reparse_point(tool_dir)
                    or not build_root.is_dir()
                    or self._is_link_or_reparse_point(build_root)
                ):
                    continue
                self._scan_tool_package_locks(add_candidate, build_root, current_time)
                self._scan_tool_recovered_locks(add_candidate, build_root, current_time)
                self._scan_tool_completed_packages(add_candidate, build_root)

    def _scan_tool_package_locks(
        self,
        add_candidate: Callable[..., None],
        build_root: Path,
        current_time: float,
    ) -> None:
        for lock_path in sorted(
            build_root.glob(".package-*.lock"),
            key=lambda item: item.name,
        ):
            if (
                not lock_path.is_dir()
                or self._is_link_or_reparse_point(lock_path)
            ):
                continue
            try:
                age_seconds = current_time - lock_path.stat(
                    follow_symlinks=False
                ).st_mtime
            except OSError:
                continue
            owner: dict[str, Any] = {}
            owner_path = lock_path / "owner.json"
            if (
                owner_path.is_file()
                and not self._is_link_or_reparse_point(owner_path)
            ):
                try:
                    loaded_owner = json.loads(
                        owner_path.read_text(encoding="utf-8")
                    )
                    if isinstance(loaded_owner, dict):
                        owner = loaded_owner
                except (OSError, json.JSONDecodeError):
                    owner = {}
            owner_pid = owner.get("pid")
            if isinstance(owner_pid, int) and self._process_is_alive(
                owner_pid
            ):
                continue
            if age_seconds >= 30:
                add_candidate(
                    lock_path,
                    layer="package",
                    anomaly="stale-package-lock",
                    reason="package owner process is no longer running",
                )


    def _scan_tool_recovered_locks(
        self,
        add_candidate: Callable[..., None],
        build_root: Path,
        current_time: float,
    ) -> None:
        for recovered_lock in sorted(
            build_root.glob("package-lock-recovery-*"),
            key=lambda item: item.name,
        ):
            try:
                age_seconds = current_time - recovered_lock.stat(
                    follow_symlinks=False
                ).st_mtime
            except OSError:
                continue
            if age_seconds >= 60 * 60:
                add_candidate(
                    recovered_lock,
                    layer="package",
                    anomaly="retired-package-lock",
                    reason="stale package lock was already retired",
                )


    def _scan_tool_completed_packages(
        self,
        add_candidate: Callable[..., None],
        build_root: Path,
    ) -> None:
        completed: list[Path] = []
        for package_root in build_root.glob("package-*"):
            if (
                not package_root.is_dir()
                or self._is_link_or_reparse_point(package_root)
                or not (
                    package_root / "promotion-complete.json"
                ).is_file()
                or not (
                    package_root
                    / "promotion-recovery-manifest.json"
                ).is_file()
            ):
                continue
            aborted_path = package_root / "promotion-aborted.json"
            if aborted_path.is_file():
                try:
                    aborted = json.loads(
                        aborted_path.read_text(encoding="utf-8")
                    )
                except (OSError, json.JSONDecodeError):
                    aborted = {}
                if (
                    isinstance(aborted, dict)
                    and aborted.get("status")
                    == "rollback-incomplete"
                ):
                    continue
            completed.append(package_root)
        completed.sort(
            key=lambda path: path.stat(
                follow_symlinks=False
            ).st_mtime_ns,
            reverse=True,
        )
        for excess in completed[1:]:
            add_candidate(
                excess,
                layer="package",
                anomaly="excess-completed-recovery",
                reason=(
                    "older successful package recovery exceeds "
                    "the one-generation retention boundary"
                ),
            )


    def _append_rule_warning_diagnostics(
        self, diagnostics: list[dict[str, Any]],
    ) -> None:
        for warning in self.rule_warnings:
            diagnostics.append(
                {
                    "path": self._relative_path(self.rules_override_path)
                    if self._inside_project(self.rules_override_path)
                    else str(self.rules_override_path),
                    "layer": "shared",
                    "anomaly": "rule-warning",
                    "repairable": False,
                    "reason": warning,
                }
            )

    def _preview_plan_invalid_reason(self, path: Path) -> str:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            expires_at = (
                _parse_iso(str(document.get("expires_at") or ""))
                if isinstance(document, dict)
                else None
            )
            valid_identity = (
                isinstance(document, dict)
                and int(document.get("schema_version") or 0)
                == PLAN_SCHEMA_VERSION
                and str(document.get("plan_id") or "") == path.stem
                and self._is_sha256_text(
                    str(document.get("plan_token") or "")
                )
            )
            if not valid_identity:
                return "invalid preview-plan document"
            if (
                expires_at is None
                or expires_at <= datetime.now(timezone.utc)
            ):
                return "expired preview plan"
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return "unreadable preview-plan document"
        return ""

    def _repair_candidate_item(
        self,
        absolute: Path,
        item_type: str,
        snapshot: dict[str, Any],
        layer: str,
        anomaly: str,
        reason: str,
    ) -> dict[str, Any]:
        return {
            "item_id": uuid.uuid4().hex,
            "path": self._relative_path(absolute),
            "type": item_type,
            "size_bytes": int(snapshot.get("size_bytes") or 0),
            "fingerprint": snapshot,
            "layer": layer,
            "anomaly": anomaly,
            "reason": reason,
            "risk": "low",
            "repairable": True,
        }
