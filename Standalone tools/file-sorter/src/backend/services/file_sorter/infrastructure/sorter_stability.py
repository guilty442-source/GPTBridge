"""Durable, no-overwrite file operations and per-target state for File Sorter.

The module intentionally has no dependency on ``main.py``.  This keeps the
transaction and profile repository usable by a future background service.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat as stat_module
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence




from .sorter_paths import (
    _is_link_or_reparse,
)
from .sorter_types import (
    DEFAULT_QUIET_SECONDS,
    FileFingerprint,
    PARTIAL_SUFFIXES,
    StabilityResult,
)


def is_partial_file(path: str | Path) -> bool:
    name = Path(path).name.casefold()
    return any(name.endswith(suffix) for suffix in PARTIAL_SUFFIXES)


def _best_effort_unlocked(path: Path) -> bool | None:
    """Return False for a detected lock and None when locking is unavailable."""

    if os.name == "nt":
        try:
            import msvcrt

            with path.open("rb") as stream:
                if path.stat().st_size == 0:
                    return True
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        return False
                    return None
                finally:
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            return True
        except PermissionError:
            return False
        except (ImportError, OSError):
            return None

    try:
        import fcntl

        with path.open("rb") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    return False
                return None
            finally:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        return True
    except (ImportError, OSError):
        return None


def check_file_stability(
    path: str | Path,
    *,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    now_ns: int | None = None,
    check_lock: bool = True,
) -> StabilityResult:
    source = Path(path)
    if is_partial_file(source):
        return StabilityResult(False, "partial-file-suffix", None)
    if _is_link_or_reparse(source):
        return StabilityResult(False, "link-or-reparse-point", None)
    try:
        stat = source.stat()
    except OSError as error:
        return StabilityResult(False, f"stat-failed:{error}", None)
    if not source.is_file():
        return StabilityResult(False, "not-a-regular-file", None)
    fingerprint = FileFingerprint(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    age_ns = (time.time_ns() if now_ns is None else now_ns) - stat.st_mtime_ns
    if age_ns < max(0.0, quiet_seconds) * 1_000_000_000:
        return StabilityResult(False, "quiet-period", fingerprint)
    if check_lock:
        unlocked = _best_effort_unlocked(source)
        if unlocked is False:
            return StabilityResult(False, "file-locked", fingerprint)
    return StabilityResult(True, None, fingerprint)


def same_volume(source: str | Path, destination_dir: str | Path) -> bool:
    try:
        return os.stat(source).st_dev == os.stat(destination_dir).st_dev
    except OSError:
        return False
