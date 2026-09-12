"""Module-level constants and exceptions for the durable sorter engine."""

from __future__ import annotations

import re
import threading
from pathlib import Path


STATE_ROOT_ENV = "FILE_SORTER_STATE_ROOT"
JOURNAL_RETENTION_DAYS_ENV = "FILE_SORTER_JOURNAL_RETENTION_DAYS"
TOOL_ROOT = Path(__file__).resolve().parents[5]
SCHEMA_VERSION = 1
DEFAULT_QUIET_SECONDS = 2.0
DEFAULT_PLAN_TTL_SECONDS = 15 * 60
DEFAULT_JOURNAL_RETENTION_DAYS = 0
DEFAULT_INCLUDE = ("*",)
DEFAULT_EXCLUDE: tuple[str, ...] = ()
PARTIAL_SUFFIXES = (
    ".crdownload",
    ".download",
    ".partial",
    ".part",
    ".tmp",
    ".temp",
    ".opdownload",
)
TERMINAL_TRANSACTION_STATES = {
    "committed",
    "undone",
    "undo_failed",
}
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{7,127}$")
_TARGET_LOCKS_GUARD = threading.RLock()
_TARGET_LOCKS_HELD: set[str] = set()


class SorterV2Error(Exception):
    """Base error for durable sorter operations."""


class RuleConflictError(SorterV2Error):
    """Raised when a profile revision changed during an update."""
