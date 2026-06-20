from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Iterable


DEFAULT_CLEANUP_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
DEFAULT_CLEANUP_SUFFIXES = {".pyc", ".pyo", ".tmp", ".temp"}
PROTECTED_DIRS = {".git", ".venv", "node_modules", "release", "dist-ui"}


def _project_root(root: str | os.PathLike[str] | None = None) -> Path:
    return Path(root or os.environ.get("GPTBRIDGE_PROJECT_ROOT", Path.cwd())).resolve()


def _is_protected(path: Path, project_root: Path) -> bool:
    try:
        rel_parts = path.resolve(strict=False).relative_to(project_root).parts
    except ValueError:
        return True
    return any(part in PROTECTED_DIRS for part in rel_parts)


def _cleanup_candidates(
    root: Path,
    *,
    cleanup_dirs: Iterable[str] = DEFAULT_CLEANUP_DIRS,
    cleanup_suffixes: Iterable[str] = DEFAULT_CLEANUP_SUFFIXES,
) -> list[Path]:
    dir_names = {item.casefold() for item in cleanup_dirs}
    suffixes = {item.casefold() for item in cleanup_suffixes}
    candidates: list[Path] = []

    for current_root, dirs, files in os.walk(root):
        current = Path(current_root)
        if _is_protected(current, root):
            dirs[:] = []
            continue

        for dirname in list(dirs):
            child = current / dirname
            if dirname.casefold() in dir_names and not _is_protected(child, root):
                candidates.append(child)
                dirs.remove(dirname)
            elif dirname in PROTECTED_DIRS:
                dirs.remove(dirname)

        for filename in files:
            child = current / filename
            if child.suffix.casefold() in suffixes and not _is_protected(child, root):
                candidates.append(child)

    return sorted(candidates, key=lambda item: (item.is_file(), str(item)))


def perform_cleanup(
    root: str | os.PathLike[str] | None = None,
    *,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Clean generated caches under the project root.

    The default is a dry-run so callers can safely inspect the plan. Set
    ``dry_run=False`` only when the caller has already confirmed the cleanup.
    """

    project_root = _project_root(root)
    candidates = _cleanup_candidates(project_root)
    items = [
        {
            "path": str(path.relative_to(project_root)),
            "type": "directory" if path.is_dir() else "file",
        }
        for path in candidates
    ]
    if dry_run:
        return {
            "ok": True,
            "dry_run": True,
            "cleaned": 0,
            "planned": len(items),
            "items": items,
        }

    removed = 0
    errors: list[dict[str, str]] = []
    for path in candidates:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            errors.append({"path": str(path.relative_to(project_root)), "error": str(exc)})

    return {
        "ok": not errors,
        "dry_run": False,
        "cleaned": removed,
        "planned": len(items),
        "items": items,
        "errors": errors,
    }
