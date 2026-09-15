"""Shared bounded helpers for the auto-repair chain.

These helpers keep the executor and the independent verifier cheap and
honest: a single ``git status`` call instead of one subprocess per file,
in-process compile checks instead of per-file ``py_compile`` children,
and bounded scope walks that never wander into vendored or generated
trees.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterator, Optional

# Directories that never carry repair evidence and would explode the
# preimage hashing cost if walked (vendored, generated, caches).
SCOPE_SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        "dist",
        "build",
    }
)

# Files larger than this are never hashed for the preimage/postimage
# evidence trail — hashing them dominates repair latency for no
# governance value (binary artifacts are covered by rebuild steps).
MAX_HASHED_FILE_BYTES: int = 5 * 1024 * 1024

_GIT_TIMEOUT_SECONDS = 10


def dirty_git_paths(project_root: Path) -> Optional[set[str]]:
    """Return the set of repo-relative dirty paths from one git call.

    ``None`` means the check could not run (git missing or the command
    failed) — callers must treat that as fail-closed ("dirty"), matching
    the source-repair contract: an unverifiable working tree is never
    assumed clean.
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    dirty: set[str] = set()
    for line in result.stdout.splitlines():
        path = line[3:] if len(line) > 3 else ""
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if path.startswith('"') and path.endswith('"'):
            try:
                path = json.loads(path)
            except (ValueError, json.JSONDecodeError):
                path = path.strip('"')
        if path:
            dirty.add(path)
    return dirty


def file_compiles(path: Path) -> tuple[bool, str]:
    """In-process syntax check — equivalent decision to ``py_compile``.

    Returns ``(ok, error_message)``.  No subprocess, no ``__pycache__``
    writes.
    """
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as error:
        return False, f"unreadable: {error.__class__.__name__}"
    try:
        compile(source, str(path), "exec")
    except (SyntaxError, ValueError) as error:
        return False, str(error)[:200]
    return True, ""


def iter_scope_files(project_root: Path, scope_rel: str) -> Iterator[tuple[str, Path]]:
    """Yield ``(repo_relative, absolute)`` file pairs inside a grant scope.

    Bounded: skip directories in :data:`SCOPE_SKIP_DIRS` and files above
    :data:`MAX_HASHED_FILE_BYTES`.
    """
    root = Path(project_root)
    full = root / scope_rel
    if full.is_file():
        yield scope_rel.replace(os.sep, "/"), full
        return
    if not full.is_dir():
        return
    for dirpath, dirnames, filenames in os.walk(full):
        dirnames[:] = [d for d in dirnames if d not in SCOPE_SKIP_DIRS]
        for name in filenames:
            candidate = Path(dirpath) / name
            try:
                if candidate.stat().st_size > MAX_HASHED_FILE_BYTES:
                    continue
            except OSError:
                continue
            yield candidate.relative_to(root).as_posix(), candidate


def atomic_write_text(path: Path, content: str) -> None:
    """Crash-safe write: temp file in the same directory then replace."""
    tmp = path.with_name(path.name + ".repair_tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def ensure_sys_path(*paths: Path) -> None:
    """Insert governance/runtime roots once — no repeated sys.path growth."""
    import sys

    for path in paths:
        entry = str(path)
        if entry not in sys.path:
            sys.path.insert(0, entry)


def step_targets(plan: Any) -> set[str]:
    """Repo-relative targets the plan's steps may legitimately modify."""
    return {
        str(step.get("target", "")).replace(os.sep, "/")
        for step in getattr(plan, "steps", [])
        if step.get("target")
    }


__all__ = [
    "MAX_HASHED_FILE_BYTES",
    "SCOPE_SKIP_DIRS",
    "atomic_write_text",
    "dirty_git_paths",
    "ensure_sys_path",
    "file_compiles",
    "iter_scope_files",
    "step_targets",
]
