# Project-cleaner scanner CleanupScannerGitMixin.
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


class CleanupScannerGitMixin:

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
                tracked = self._run_git_ls_files()
                self._git_tracked_cache = tracked
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

        def _run_git_ls_files(self) -> set[str]:
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
            return tracked
