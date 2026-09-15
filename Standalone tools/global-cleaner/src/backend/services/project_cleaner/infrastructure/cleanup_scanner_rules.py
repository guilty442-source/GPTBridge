# Project-cleaner scanner CleanupScannerRulesMixin.
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


class CleanupScannerRulesMixin:

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
