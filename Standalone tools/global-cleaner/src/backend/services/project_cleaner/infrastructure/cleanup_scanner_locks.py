# Project-cleaner scanner CleanupScannerLocksMixin.
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


class CleanupScannerLocksMixin:

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
                    return self._restart_manager_locks(
                        restart_manager, session, path, RM_PROCESS_INFO, wintypes
                    )
                finally:
                    restart_manager.RmEndSession(session)
            except Exception:
                return []

        @staticmethod
        def _restart_manager_locks(
            restart_manager: Any,
            session: Any,
            path: Path,
            rm_process_info: type,
            wintypes: Any,
        ) -> list[dict[str, Any]]:
            resources = (wintypes.LPCWSTR * 1)(str(path))
            if restart_manager.RmRegisterResources(session, 1, resources, 0, None, 0, None) != 0:
                return []
            needed = wintypes.UINT(0)
            count = wintypes.UINT(0)
            reason = wintypes.DWORD(0)
            result = restart_manager.RmGetList(
                session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reason)
            )
            if result != 234 or needed.value == 0:
                return []
            entries = (rm_process_info * needed.value)()
            count = wintypes.UINT(needed.value)
            if restart_manager.RmGetList(
                session, ctypes.byref(needed), ctypes.byref(count), entries, ctypes.byref(reason)
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
