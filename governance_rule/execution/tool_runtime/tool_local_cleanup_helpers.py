"""Tool local cleanup helpers (A185 split).

Contains the directory cleanup, file cleanup, and empty directory sweep
helpers extracted from _emit_cleanup.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

from .tool_local_cleanup import (
    DIRECTORY_RULES,
    EXCLUDED_DIRECTORY_NAMES,
    FILE_RULES,
    LocalCleanupResult,
    _dir_size,
    _emit_contents,
    _inside,
    _iso_now,
    _remove_path,
)


def _cleanup_matching_directories(
    tool_root: Path,
    walk_root: str,
    directory_names: list[str],
    now: float,
    git_tracked: frozenset[str] | None,
    age_days_fn: Any,
    is_protected_fn: Any,
    cleaned_directories: list[str],
    skipped: list[dict[str, str]],
    cleaned_bytes_ref: list[int],
    error_count_ref: list[int],
    *,
    byte_quota: int | None = None,
) -> list[str]:
    """Process matching directories for cleanup. Returns kept_directories."""
    current_raw = Path(walk_root)
    kept_directories: list[str] = []
    for name in directory_names:
        candidate = current_raw / name
        relative_dir = (current_raw / name).relative_to(tool_root)
        matched_rule = next(
            (
                rule
                for rule in DIRECTORY_RULES
                if (
                    rule.get("names")
                    and name.casefold()
                    in {str(item).casefold() for item in rule["names"]}
                )
                or (
                    rule.get("relative_patterns")
                    and f"/{relative_dir.as_posix()}/" in {
                        f"/{str(pattern).strip('/')}/"
                        for pattern in rule["relative_patterns"]
                    }
                )
            ),
            None,
        )
        if matched_rule is not None:
            if not _inside(candidate, tool_root):
                continue
            min_age_days = max(
                0.0, float(matched_rule.get("min_age_days") or 0)
            )
            if age_days_fn(candidate, now) < min_age_days:
                continue
            dir_prefix = f"{relative_dir.as_posix().rstrip(chr(47))}/"
            if git_tracked is None:
                skipped.append(
                    {
                        "path": relative_dir.as_posix(),
                        "reason": "git protection unavailable",
                    }
                )
                continue
            if any(item.startswith(dir_prefix) for item in git_tracked):
                skipped.append(
                    {
                        "path": relative_dir.as_posix(),
                        "reason": "directory contains git-tracked files",
                    }
                )
                continue
            directory_bytes = _dir_size(candidate)
            if (
                byte_quota is not None
                and cleaned_bytes_ref[0] + directory_bytes > byte_quota
            ):
                skipped.append(
                    {
                        "path": relative_dir.as_posix(),
                        "reason": "cleanup byte quota reached",
                    }
                )
                continue
            try:
                if matched_rule.get("contents_only"):
                    _emit_contents(candidate, tool_root)
                    cleaned_bytes_ref[0] += directory_bytes
                    cleaned_directories.append(
                        relative_dir.as_posix()
                    )
                    continue
                cleaned_bytes_ref[0] += directory_bytes
                _remove_path(candidate)
                cleaned_directories.append(relative_dir.as_posix())
            except OSError as error:
                error_count_ref[0] += 1
                skipped.append(
                    {
                        "path": relative_dir.as_posix(),
                        "reason": f"{type(error).__name__}: {error}",
                    }
                )
            continue
        if name.casefold() in EXCLUDED_DIRECTORY_NAMES:
            continue
        kept_directories.append(name)
    return kept_directories


def _cleanup_matching_files(
    tool_root: Path,
    walk_root: str,
    file_names: list[str],
    now: float,
    git_tracked: frozenset[str] | None,
    age_days_fn: Any,
    cleaned_files: list[str],
    skipped: list[dict[str, str]],
    cleaned_bytes_ref: list[int],
    error_count_ref: list[int],
    *,
    byte_quota: int | None = None,
) -> None:
    """Process matching files for cleanup."""
    current_raw = Path(walk_root)
    for name in file_names:
        candidate = current_raw / name
        if not _inside(candidate, tool_root):
            continue
        rule = next(
            (
                rule
                for rule in FILE_RULES
                if any(
                    candidate.name.casefold().endswith(
                        pattern.lstrip("*").casefold()
                    )
                    for pattern in rule["patterns"]
                )
            ),
            None,
        )
        if rule is None:
            continue
        relative_file = candidate.relative_to(tool_root).as_posix()
        if git_tracked is None:
            skipped.append(
                {
                    "path": relative_file,
                    "reason": "git protection unavailable",
                }
            )
            continue
        if relative_file in git_tracked:
            continue
        age_days = age_days_fn(candidate, now)
        if age_days < float(rule["min_age_days"]):
            continue
        try:
            file_bytes = candidate.stat(follow_symlinks=False).st_size
        except OSError as error:
            error_count_ref[0] += 1
            skipped.append(
                {
                    "path": relative_file,
                    "reason": f"{type(error).__name__}: {error}",
                }
            )
            continue
        if (
            byte_quota is not None
            and cleaned_bytes_ref[0] + file_bytes > byte_quota
        ):
            skipped.append(
                {
                    "path": relative_file,
                    "reason": "cleanup byte quota reached",
                }
            )
            continue
        try:
            cleaned_bytes_ref[0] += file_bytes
            _remove_path(candidate)
            cleaned_files.append(relative_file)
        except OSError as error:
            error_count_ref[0] += 1
            skipped.append(
                {
                    "path": relative_file,
                    "reason": f"{type(error).__name__}: {error}",
                }
            )


def _sweep_empty_directories(
    tool_root: Path,
    can_sweep_empty_fn: Any,
    cleaned_directories: list[str],
    skipped: list[dict[str, str]],
    error_count_ref: list[int],
) -> None:
    """Sweep empty directories after file cleanup."""
    for walk_root, directory_names, file_names in os.walk(
        tool_root,
        topdown=False,
        followlinks=False,
    ):
        current_raw = Path(walk_root)
        try:
            relative = current_raw.relative_to(tool_root)
        except ValueError:
            continue
        if current_raw == tool_root or not can_sweep_empty_fn(relative):
            continue
        if current_raw.is_symlink():
            continue
        try:
            if any(current_raw.iterdir()):
                continue
        except OSError:
            continue
        try:
            _remove_path(current_raw)
            cleaned_directories.append(relative.as_posix())
        except OSError as error:
            error_count_ref[0] += 1
            skipped.append(
                {
                    "path": relative.as_posix(),
                    "reason": f"{type(error).__name__}: {error}",
                }
            )


__all__ = [
    "_cleanup_matching_directories",
    "_cleanup_matching_files",
    "_sweep_empty_directories",
]
