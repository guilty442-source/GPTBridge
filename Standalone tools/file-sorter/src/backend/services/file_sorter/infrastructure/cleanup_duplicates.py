"""Exact-duplicate detection and recycle-bin operations."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Sequence

from .cleanup_constants import PROGRESS_JSON_PREFIX
from .cleanup_errors import CleanupError
from .cleanup_utils import (
    _is_link_or_reparse,
    _iter_contained_regular_files,
    _resolve_target_dir,
    _sha256_file,
)


def find_exact_duplicate_candidates(
    target_dir: str | Path,
    *,
    quiet_seconds: float = 2.0,
) -> list[dict[str, Any]]:
    """Return exact duplicate candidates while preserving one canonical copy.

    Files must be non-empty, unchanged for the quiet period, and byte-for-byte
    identical by SHA-256. Hard links to the same file are not considered
    duplicates because recycling one would not reclaim storage.
    """

    target = _resolve_target_dir(target_dir)
    now_ns = time.time_ns()
    by_size: dict[int, list[tuple[Path, int]]] = {}
    for path in _iter_contained_regular_files(target):
        if path.name.casefold().endswith(
            (".crdownload", ".download", ".partial", ".part", ".tmp", ".temp")
        ):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size <= 0:
            continue
        if now_ns - stat.st_mtime_ns < max(0.0, quiet_seconds) * 1_000_000_000:
            continue
        by_size.setdefault(int(stat.st_size), []).append((path, int(stat.st_mtime_ns)))

    candidates: list[dict[str, Any]] = []
    for size, sized_files in by_size.items():
        if len(sized_files) < 2:
            continue
        by_digest: dict[str, list[tuple[Path, int]]] = {}
        for path, mtime_ns in sized_files:
            try:
                digest = _sha256_file(path)
            except OSError:
                continue
            by_digest.setdefault(digest, []).append((path, mtime_ns))
        for digest, matches in by_digest.items():
            if len(matches) < 2:
                continue
            ordered = sorted(
                matches,
                key=lambda item: (
                    item[1],
                    str(item[0].relative_to(target)).casefold(),
                ),
            )
            keep, _keep_mtime_ns = ordered[0]
            for duplicate, duplicate_mtime_ns in ordered[1:]:
                try:
                    if os.path.samefile(keep, duplicate):
                        continue
                except OSError:
                    continue
                candidates.append(
                    {
                        "path": str(duplicate),
                        "relative_path": str(duplicate.relative_to(target)),
                        "keep_path": str(keep),
                        "keep_relative_path": str(keep.relative_to(target)),
                        "size": size,
                        "mtime_ns": duplicate_mtime_ns,
                        "sha256": digest,
                    }
                )
    return candidates


def _send_to_windows_recycle_bin(path: Path) -> None:
    if os.name != "nt":
        raise CleanupError("Automatic duplicate recycling is supported only on Windows.")

    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", wintypes.UINT),
            ("pFrom", wintypes.LPCWSTR),
            ("pTo", wintypes.LPCWSTR),
            ("fFlags", wintypes.WORD),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", wintypes.LPCWSTR),
        ]

    operation = SHFILEOPSTRUCTW()
    operation.wFunc = 0x0003  # FO_DELETE
    operation.pFrom = str(path) + "\0\0"
    operation.fFlags = 0x0040 | 0x0010 | 0x0004 | 0x0400
    result = int(ctypes.windll.shell32.SHFileOperationW(ctypes.byref(operation)))
    if result != 0 or operation.fAnyOperationsAborted:
        raise CleanupError(f"Windows Recycle Bin rejected the file (code {result}).")


def recycle_exact_duplicate_candidates(
    target_dir: str | Path,
    candidates: Sequence[dict[str, Any]],
    *,
    recycler: Callable[[Path], None] = _send_to_windows_recycle_bin,
) -> dict[str, Any]:
    """Revalidate exact duplicates and send only proven extras to Recycle Bin."""

    target = _resolve_target_dir(target_dir)
    recycled: list[dict[str, Any]] = []
    errors: list[str] = []
    for candidate in candidates:
        duplicate = Path(str(candidate.get("path") or ""))
        keep = Path(str(candidate.get("keep_path") or ""))
        expected_hash = str(candidate.get("sha256") or "")
        expected_size = int(candidate.get("size") or -1)
        expected_mtime_ns = int(candidate.get("mtime_ns") or -1)
        try:
            for path in (duplicate, keep):
                resolved = path.resolve(strict=True)
                resolved.relative_to(target)
                if _is_link_or_reparse(path) or not resolved.is_file():
                    raise CleanupError("Duplicate candidate is no longer a regular file.")
            duplicate_stat = duplicate.stat()
            if (
                duplicate_stat.st_size != expected_size
                or duplicate_stat.st_mtime_ns != expected_mtime_ns
            ):
                raise CleanupError("Duplicate candidate changed after observation.")
            if os.path.samefile(duplicate, keep):
                raise CleanupError("Duplicate candidate is a hard link to the retained file.")
            if not expected_hash or _sha256_file(keep) != expected_hash:
                raise CleanupError("Retained file changed after observation.")
            if _sha256_file(duplicate) != expected_hash:
                raise CleanupError("Duplicate content changed after observation.")
            recycler(duplicate)
            if duplicate.exists():
                raise CleanupError("Recycle Bin operation did not remove the source path.")
            recycled.append(
                {
                    "path": str(duplicate),
                    "keep_path": str(keep),
                    "size": expected_size,
                    "sha256": expected_hash,
                }
            )
        except (CleanupError, OSError, TypeError, ValueError) as error:
            errors.append(f"{duplicate.name or 'unknown'}: {error}")
    return {
        "ok": not errors,
        "type": "file-sorter-duplicate-recycle-result",
        "target_dir": str(target),
        "recycled_count": len(recycled),
        "recycled": recycled,
        "errors": errors,
    }


def print_progress_event(event: dict[str, Any]) -> None:
    print(
        PROGRESS_JSON_PREFIX
        + json.dumps(event, ensure_ascii=False, separators=(",", ":")),
        flush=True,
    )
