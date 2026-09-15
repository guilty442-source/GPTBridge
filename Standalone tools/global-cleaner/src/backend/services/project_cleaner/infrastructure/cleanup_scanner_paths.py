# Project-cleaner scanner CleanupScannerPathsMixin.
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


class CleanupScannerPathsMixin:

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
