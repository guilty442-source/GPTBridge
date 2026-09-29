"""Shared vocabulary for the codex update pipeline: constants, records, file utilities."""
from __future__ import annotations

import hashlib
import json
import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Mapping

from governance_rule.execution.chinese_codex_mirror import PART_NAMES
from governance_rule.execution.codex_postgresql_stage import open_codex_store
from governance_rule.execution.codex_update_validation import (
    foreign_key_violations,
)


UPDATE_FLOW_IDENTITY: Final[str] = "A382/A488/A537/A538-isolate-change-wire-release-refresh"
UPDATE_PHASES: Final[tuple[str, ...]] = (
    "isolate",
    "execute-change",
    "wire",
    "release-isolation",
    "frontend-refresh",
)
DATABASE_NAME: Final[str] = "governance_codex.sql"
ISOLATION_MARKER: Final[str] = "STAGE_ISOLATION.json"
RELEASED_MARKER: Final[str] = "STAGE_RELEASED.json"
REFRESH_REQUEST: Final[str] = "FRONTEND_REFRESH.json"
FRONTEND_REFRESH_CHANNELS: Final[tuple[str, ...]] = (
    "frontend",
    "backend",
    "ui-projection",
)


class CodexUpdateError(RuntimeError):
    """Fail-closed denial or failure inside one update phase."""

    def __init__(self, phase: str, reason: str) -> None:
        super().__init__(f"{phase}: {reason}")
        self.phase = phase
        self.reason = reason


@dataclass(frozen=True)
class PhaseRecord:
    phase: str
    ok: bool
    detail: str
    evidence: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class IsolatedStage:
    fence_id: str
    codex_root: Path
    staging_root: Path
    database: Path
    parts: tuple[Path, ...]
    source_version: str
    source_digests: Mapping[str, str]
    source_fk_violations: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class AutoUpdateResult:
    ok: bool
    applied: bool
    version: str
    phases: tuple[PhaseRecord, ...]
    refresh_pending: bool = False


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _set_read_only(path: Path, read_only: bool = True) -> None:
    """Set or clear the read-only attribute using Windows API.

    On Windows, os.chmod with stat.S_IWRITE is not reliable for the
    FILE_ATTRIBUTE_READONLY flag. Use ctypes to call SetFileAttributesW
    directly for consistent behaviour across platforms.
    """
    try:
        import ctypes
        from ctypes import wintypes

        FILE_ATTRIBUTE_READONLY = 0x00000001
        kernel32 = ctypes.windll.kernel32

        path_str = str(path.resolve())
        attrs = kernel32.GetFileAttributesW(path_str)
        if attrs == 0xFFFFFFFF:
            raise OSError(f"GetFileAttributesW failed for {path_str}")

        if read_only:
            new_attrs = attrs | FILE_ATTRIBUTE_READONLY
        else:
            new_attrs = attrs & ~FILE_ATTRIBUTE_READONLY

        if not kernel32.SetFileAttributesW(path_str, new_attrs):
            raise OSError(f"SetFileAttributesW failed for {path_str}")
    except (ImportError, AttributeError, OSError):
        # Fallback to os.chmod for non-Windows or if ctypes fails
        mode = path.stat().st_mode
        os.chmod(path, mode & ~stat.S_IWRITE if read_only else mode | stat.S_IWRITE)


def _atomic_replace(source: Path, target: Path) -> None:
    """temp -> fsync -> replace -> read-only, never leaving a half file."""
    temporary = target.with_name(target.name + ".staging-tmp")
    with temporary.open("wb") as handle:
        handle.write(source.read_bytes())
        handle.flush()
        os.fsync(handle.fileno())
    if target.exists():
        _set_read_only(target, False)
    # Windows readers may hold the target without delete sharing for a
    # short window (indexers, watchers); retry briefly before failing.
    last_error: OSError | None = None
    for _ in range(10):
        try:
            os.replace(temporary, target)
            last_error = None
            break
        except PermissionError as error:
            last_error = error
            time.sleep(0.5)
    if last_error is not None:
        try:
            temporary.unlink()
        except OSError:
            pass
        if target.exists():
            _set_read_only(target, True)
        raise last_error
    _set_read_only(target, True)


def _write_json(path: Path, payload: Mapping[str, object]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _read_version(database: Path) -> str:
    with open_codex_store(database) as connection:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key='codex_version'"
        ).fetchone()
        return str(row[0]).strip() if row else ""


def _source_foreign_key_violations(database: Path) -> tuple[tuple[str, ...], ...]:
    """Baseline: the live generation's acknowledged legacy violations."""
    with open_codex_store(database) as connection:
        return foreign_key_violations(connection)


def _canonical_codex_root() -> Path:
    return Path(__file__).resolve().parents[2] / "governance_rule" / "codex"


def _same_path(left: Path, right: Path) -> bool:
    return str(left).casefold() == str(right).casefold()
