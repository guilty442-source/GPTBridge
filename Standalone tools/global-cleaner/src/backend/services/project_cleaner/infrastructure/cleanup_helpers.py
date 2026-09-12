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


SECONDS_PER_DAY = 24 * 60 * 60
LEGACY_QUARANTINE_ROOT_NAME = ".GPTBridge_CleanerQuarantine"
LEGACY_RECOVERY_ROOT_NAME = ".GPTBridge_CleanerRecovery"
QUARANTINE_RELATIVE_PATH = (
    Path("global-cleaner") / "runtime" / "quarantine"
)
RECOVERY_RELATIVE_PATH = Path("global-cleaner") / "runtime" / "recovery"
CLEANER_RUNTIME_RELATIVE_PATH = (
    Path("global-cleaner") / "runtime" / "state" / "cleanup"
)
DEFAULT_QUARANTINE_TTL_HOURS = 24
DEFAULT_PLAN_TTL_MINUTES = 15
MAX_REPORTED_SKIPS = 200
MAX_HISTORY_RECORDS = 200
PROGRESS_JSON_PREFIX = "PROJECT_CLEANER_PROGRESS_JSON="
MANAGED_BACKUP_SCHEMA_VERSION = 1
MANAGED_BACKUP_RETENTION_PER_OWNER = 1
MANAGED_BACKUP_RELATIVE_ROOT = (
    Path("global-cleaner") / "data" / "business" / "backups"
)
BACKUP_EXTRACT_RELATIVE_ROOT = (
    Path("global-cleaner")
    / "runtime"
    / "temp"
    / "shared-layer"
    / "backup-extract"
)
SYSTEM_RESCUE_REQUIRED_PATHS = (
    "main-system/src-core/main.py",
    "main-system/src-core/ipc/server.py",
    "main-system/src-core/ipc/handlers.py",
    "main-system/config/tool-runtime-contract.json",
    "main-system/package.json",
    "system-rescue/src/backend/services/system_rescue/integration/platform_packager.py",
)
SYSTEM_RESCUE_REPAIR_ANOMALIES = frozenset(
    {
        "invalid-production-signature",
        "orphan-atomic-temp",
        "stale-package-lock",
        "stale-preview-plan",
    }
)


def _background_subprocess_kwargs() -> dict[str, int]:
    if os.name != "nt":
        return {}
    creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
    return {"creationflags": creationflags} if creationflags else {}


PLAN_SCHEMA_VERSION = 1
QUARANTINE_SCHEMA_VERSION = 1
CORE_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "browser-profile",
        "browser-profiles",
        "edge-profile",
        "backups",
    }
)
CORE_PROTECTED_RELATIVE_PATHS = frozenset(
    {
        "runtime/profiles",
        "runtime/state",
        "runtime/browser-profiles",
        "runtime/file-sorter",
        "runtime/ipc",
        "dist-ui",
        "release",
        "browser-profile",
        "edge-profile",
        "backups",
        "global-cleaner/runtime/state",
        QUARANTINE_RELATIVE_PATH.as_posix(),
        RECOVERY_RELATIVE_PATH.as_posix(),
    }
)
SOURCE_LIKE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".php",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
ProgressCallback = Callable[[dict[str, Any]], None]


# The in-process lock closes a gap in platforms where file-lock semantics are
# process-scoped. The OS lock below is still authoritative across processes.
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


FALLBACK_RULES: dict[str, Any] = {
    "schema_version": 1,
    "delete_files_only": True,
    "plan_ttl_minutes": DEFAULT_PLAN_TTL_MINUTES,
    "quarantine_ttl_hours": DEFAULT_QUARANTINE_TTL_HOURS,
    "excluded_directory_names": [
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "browser-profile",
        "edge-profile",
        "backups",
    ],
    "protected_relative_paths": [
        "runtime/profiles",
        "runtime/state",
        "browser-profile",
        "edge-profile",
        "backups",
        QUARANTINE_RELATIVE_PATH.as_posix(),
        RECOVERY_RELATIVE_PATH.as_posix(),
    ],
    "directory_rules": [
        {
            "id": "governed-temporary-storage",
            "relative_patterns": [
                "global-cleaner/runtime/temp",
            ],
            "reason": "governed ephemeral temporary storage",
            "risk": "low",
            "min_age_days": 0,
            "protect_user_content": False,
            "contents_only": True,
        },
        {
            "id": "legacy-root-cleaner-runtime",
            "relative_patterns": ["runtime/global-cleaner"],
            "reason": "legacy root-level cleaner runtime replaced by tool-owned state",
            "risk": "medium",
            "min_age_days": 0,
            "protect_user_content": False,
            "contents_only": True,
            "allow_direct_delete": True,
        },
        {
            "id": "legacy-main-edge-profile",
            "relative_patterns": ["main-system/src-core/edge-profile"],
            "reason": "obsolete Edge runtime profile stored inside source code",
            "risk": "medium",
            "min_age_days": 0,
            "protect_user_content": False,
            "contents_only": True,
            "allow_direct_delete": True,
        },
        {
            "id": "visual-smoke-sandbox",
            "patterns": ["gptbridge-ai-assistant-visual-*"],
            "reason": "isolated visual smoke sandbox",
            "risk": "low",
            "min_age_days": 0,
            "protect_user_content": False,
            "sandbox_only": True,
        },
        {
            "id": "python-bytecode",
            "names": ["__pycache__"],
            "reason": "python bytecode cache",
            "risk": "low",
            "min_age_days": 0,
            "protect_user_content": False,
        },
        {
            "id": "tool-cache",
            "names": [".pytest_cache", ".mypy_cache", ".ruff_cache"],
            "reason": "tool cache directory",
            "risk": "low",
            "min_age_days": 0,
            "protect_user_content": False,
        },
        {
            "id": "generic-cache",
            "names": ["cache", "temp", "tmp"],
            "reason": "generated working directory",
            "risk": "low",
            "min_age_days": 0,
            "protect_user_content": True,
        },
    ],
    "file_rules": [
        {
            "id": "legacy-shared-channel-database",
            "relative_patterns": [
                "shared-layer/data/shared-layer.sqlite3",
                "shared-layer/data/shared-layer.sqlite3-wal",
                "shared-layer/data/shared-layer.sqlite3-shm",
            ],
            "reason": "legacy combined channel database replaced by isolated channels",
            "risk": "medium",
            "min_age_days": 0,
            "allow_tracked": True,
            "allow_direct_delete": True,
        },
        {
            "id": "obsolete-cleaner-history",
            "relative_patterns": ["global-cleaner/data/business/history.sqlite3"],
            "reason": "obsolete global cleaner business history",
            "risk": "medium",
            "min_age_days": 0,
            "allow_direct_delete": True,
        },
        {
            "id": "temporary",
            "patterns": ["*.tmp", "*.temp"],
            "reason": "temporary file",
            "risk": "low",
            "min_age_days": 1,
        },
        {
            "id": "old-log",
            "patterns": ["*.log"],
            "reason": "expired log file",
            "risk": "low",
            "min_age_days": 7,
        },
        {
            "id": "old-copy",
            "patterns": ["*.old", "*.bak"],
            "reason": "old file copy",
            "risk": "medium",
            "min_age_days": 14,
        },
    ],
    "analysis": {
        "duplicate_min_size_bytes": 1024 * 1024,
        "large_file_min_size_bytes": 10 * 1024 * 1024,
        "large_file_min_age_days": 14,
        "hash_chunk_bytes": 1024 * 1024,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
