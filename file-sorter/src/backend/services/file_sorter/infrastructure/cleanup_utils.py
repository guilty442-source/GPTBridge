"""Filesystem utility helpers for cleanup scanning."""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path

from .cleanup_constants import TOOL_ROOT
from .cleanup_errors import CleanupError


def _default_state_root() -> Path:
    explicit = str(os.environ.get("FILE_SORTER_STATE_ROOT") or "").strip()
    candidate = (
        Path(explicit).expanduser().resolve()
        if explicit
        else TOOL_ROOT / "data" / "business"
    )
    if not candidate.is_relative_to(TOOL_ROOT):
        raise PermissionError("FILE_SORTER_DATABASE_SCOPE_DENIED")
    return candidate


def _configure_utf8_stdio() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            continue


def _clamp_percent(value: int | float | None, default: int) -> int:
    if value is None:
        return default
    return max(1, min(100, int(round(float(value)))))


def _safe_relative_path(path: Path, root: Path) -> Path:
    try:
        relative = path.resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return Path(path.name)
    return Path(*[part for part in relative.parts if part not in {"", ".", ".."}])


def _relative_path_text(path: Path, root: Path) -> str:
    return str(_safe_relative_path(path, root))


def _is_link_or_reparse(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        return True
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    return path.is_symlink() or bool(attributes & 0x400)


def _resolve_target_dir(target_dir: str | Path) -> Path:
    requested = Path(target_dir).expanduser()
    if _is_link_or_reparse(requested):
        raise CleanupError("Target folder cannot be a link or reparse point.")
    try:
        target = requested.resolve(strict=True)
    except OSError as error:
        raise CleanupError(
            f"Target folder cannot be resolved: {requested}: {error}"
        ) from error
    if _is_link_or_reparse(target):
        raise CleanupError("Target folder cannot be a link or reparse point.")
    if not target.is_dir():
        raise CleanupError(f"Target folder does not exist: {target}")
    return target


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _iter_contained_regular_files(target: Path) -> list[Path]:
    files: list[Path] = []
    excluded_names = {
        ".git",
        "__pycache__",
        "_cleaner_backup",
        ".gptbridge_cleanerquarantine",
    }
    for current_root, directory_names, file_names in os.walk(
        target,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        directory_names[:] = [
            name
            for name in directory_names
            if name.casefold() not in excluded_names
            and not _is_link_or_reparse(current / name)
        ]
        for name in file_names:
            path = current / name
            if _is_link_or_reparse(path):
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(target)
            except (OSError, ValueError):
                continue
            if resolved.is_file():
                files.append(resolved)
    return sorted(
        files,
        key=lambda path: str(path.relative_to(target)).casefold(),
    )
