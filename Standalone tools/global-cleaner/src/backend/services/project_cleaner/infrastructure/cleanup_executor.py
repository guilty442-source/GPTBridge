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

from .cleanup_executor_legacy import CleanupLegacyMixin
from .cleanup_executor_backup import CleanupBackupMixin
from .cleanup_executor_locks import CleanupLocksMixin
from .cleanup_executor_records import CleanupRecordsMixin
from .cleanup_executor_garbage import CleanupGarbageMixin
from .cleanup_executor_quarantine import CleanupQuarantineMixin
from .cleanup_executor_repair import CleanupRepairMixin


class CleanupExecutorMixin(
    CleanupLegacyMixin,
    CleanupBackupMixin,
    CleanupLocksMixin,
    CleanupRecordsMixin,
    CleanupGarbageMixin,
    CleanupQuarantineMixin,
    CleanupRepairMixin,
):

        def __init__(
            self,
            project_root: Path,
            *,
            progress_callback: ProgressCallback | None = None,
        ) -> None:
            self.project_root = project_root.resolve()
            self.runtime_root = self.project_root / CLEANER_RUNTIME_RELATIVE_PATH
            self.data_root = self.runtime_root / "business"
            self.backup_root = self.project_root / MANAGED_BACKUP_RELATIVE_ROOT
            self.backup_extract_root = (
                self.project_root / BACKUP_EXTRACT_RELATIVE_ROOT
            )
            self.quarantine_root = self.project_root / QUARANTINE_RELATIVE_PATH
            self.recovery_root = self.project_root / RECOVERY_RELATIVE_PATH
            self.plan_root = self.runtime_root / "plans"
            self.business_history = BusinessHistoryStore(
                self.data_root / "history.sqlite3",
            )
            self.key_path = self.runtime_root / "plan.key"
            self.rules_override_path = self.project_root / "config" / "global-cleaner-rules.json"
            self.progress_callback = progress_callback
            self._hardened_private_paths: set[str] = set()
            self.rules, self.rule_warnings = self._load_rules()
            self._git_tracked_cache: set[str] | None = None
            self._git_status_cache: dict[str, Any] | None = None
