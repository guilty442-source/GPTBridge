from __future__ import annotations

import json
import os
import shutil
import stat as stat_module
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SECONDS_PER_DAY = 24 * 60 * 60

CLEANUP_VERSION: str = "1.00000"

EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "dist",
        "dist-ui",
        "release",
        "browser-profile",
        "browser-profiles",
        "edge-profile",
        "backups",
        "electron-user-data",
    }
)

PROTECTED_RELATIVE_DIRECTORIES = (
    "runtime/ipc",
    "runtime/state",
    "runtime/recovery",
    "runtime/profiles",
    "data/business",
)

# Low-risk, tool-root-local only operations. Mirrors the global cleaner's safe
# policy but is constrained to the owning tool's own root and never touches
# any other module, governance data, backups, or git-tracked content.
DIRECTORY_RULES: list[dict[str, Any]] = [
    {
        "id": "tool-runtime-temp",
        "relative_patterns": ["runtime/temp"],
        "reason": "tool-owned ephemeral temporary storage",
        "min_age_days": 0,
        "contents_only": True,
    },
    {
        "id": "python-bytecode",
        "names": ["__pycache__"],
        "reason": "python bytecode cache",
        "min_age_days": 0,
        "contents_only": False,
    },
    {
        "id": "tool-cache",
        "names": [".pytest_cache", ".mypy_cache", ".ruff_cache"],
        "reason": "tool cache directory",
        "min_age_days": 0,
        "contents_only": False,
    },
]

# contents_only 規則的錨點目錄：即使清空也保留目錄本身，空目錄清掃不得移除。
EMPTY_SWEEP_ANCHORS: frozenset[str] = frozenset(
    str(Path(pattern)).replace("\\", "/")
    for rule in DIRECTORY_RULES
    if rule.get("contents_only")
    for pattern in rule.get("relative_patterns", ())
)


FILE_RULES: list[dict[str, Any]] = [
    {
        "id": "temporary",
        "patterns": ["*.tmp", "*.temp"],
        "reason": "temporary file",
        "min_age_days": 1,
    },
    {
        "id": "old-log",
        "patterns": ["*.log"],
        "reason": "expired log file",
        "min_age_days": 7,
    },
]


LOCAL_CLEANUP_STATE_RELATIVE_PATH = (
    Path("runtime") / "state" / "local-cleanup.json"
)


def local_cleanup_state_path(tool_root: Path | str) -> Path:
    return Path(tool_root) / LOCAL_CLEANUP_STATE_RELATIVE_PATH


def write_local_cleanup_state(
    tool_root: Path | str, result: dict[str, Any]
) -> None:
    """Persist the latest local-cleanup result for stopped-module oversight.

    ``runtime/state`` is protected from cleanup rules, so the record survives
    later sweeps. Best-effort: persistence failure never fails the cleanup.
    """

    state_path = local_cleanup_state_path(tool_root)
    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, state_path)
    except OSError:
        pass


def read_local_cleanup_state(tool_root: Path | str) -> dict[str, Any] | None:
    """Return the persisted local-cleanup result, or ``None`` if absent."""

    try:
        payload = json.loads(
            local_cleanup_state_path(tool_root).read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _inside(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _background_subprocess_kwargs() -> dict[str, int]:
    if os.name != 'nt':
        return {}
    creationflags = int(getattr(subprocess, 'CREATE_NO_WINDOW', 0) or 0)
    return {'creationflags': creationflags} if creationflags else {}


def _clear_readonly_and_retry(
    operation: Any,
    path: str,
    _error: tuple[type[BaseException], BaseException, Any],
) -> None:
    os.chmod(path, stat_module.S_IWRITE)
    operation(path)


def _remove_path(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path, onerror=_clear_readonly_and_retry)
    else:
        path.unlink(missing_ok=True)


def _emit_contents(path: Path, tool_root: Path) -> None:
    """Remove the children of an owned directory, keeping the directory itself."""
    for child in path.iterdir():
        if not _inside(child, tool_root):
            continue
        _remove_path(child)


@dataclass(frozen=True)
class LocalCleanupResult:
    ok: bool
    operation: str
    authority: str
    version: str
    tool_id: str
    cleaned_files: list[str]
    cleaned_directories: list[str]
    skipped: list[dict[str, str]]
    cleaned_bytes: int
    started_at: str
    completed_at: str
    dead_code_scan: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self)}


class ToolLocalCleanup:
    """Bounded, tool-root-local low-risk garbage cleanup.

    Every candidate is forced to resolve within ``tool_root`` and is rejected
    unless it is one of the explicitly declared safe patterns below. It never
    touches other modules, ejects protected directories, follows links, or
    removes git-tracked content.
    """

    def __init__(
        self,
        tool_id: str,
        tool_root: Path | str,
        *,
        max_cleaned_bytes: int | None = None,
    ) -> None:
        self.tool_id = tool_id
        self.tool_root = Path(tool_root).resolve()
        self.max_cleaned_bytes = (
            None
            if max_cleaned_bytes is None
            else max(0, int(max_cleaned_bytes))
        )
        self._git_tracked: frozenset[str] | None = None
        self._git_tracked_loaded = False

    def _is_protected(self, relative: Path) -> bool:
        """True when relative is a protected directory or inside one.

        Ancestors of protected paths are NOT protected -- the walk must
        still descend through them to reach the boundary (runtime
        itself is walkable; runtime/state and below are not).
        """
        for protected in PROTECTED_RELATIVE_DIRECTORIES:
            protected_path = Path(protected)
            if relative == protected_path or protected_path in relative.parents:
                return True
        for excluded in EXCLUDED_DIRECTORY_NAMES:
            if excluded in relative.parts:
                return True
        return False

    def _repo_marker_exists(self) -> bool:
        for ancestor in (self.tool_root, *self.tool_root.parents):
            if (ancestor / '.git').exists():
                return True
        return False

    def _git_tracked_paths(self) -> frozenset[str] | None:
        """Tracked paths under tool_root (relative), or None when a
        worktree exists but tracking cannot be determined -- fail-closed so
        git-tracked content is never treated as garbage."""
        if self._git_tracked_loaded:
            return self._git_tracked
        self._git_tracked_loaded = True
        if not self._repo_marker_exists():
            self._git_tracked = frozenset()
            return self._git_tracked
        try:
            completed = subprocess.run(
                ['git', '-C', str(self.tool_root), 'ls-files', '-z'],
                capture_output=True,
                timeout=10,
                check=False,
                **_background_subprocess_kwargs(),
            )
            if completed.returncode != 0:
                self._git_tracked = None
                return self._git_tracked
            self._git_tracked = frozenset(
                item.replace(chr(92), '/')
                for item in completed.stdout.decode(
                    'utf-8', errors='surrogateescape'
                ).split(chr(0))
                if item
            )
        except (OSError, subprocess.SubprocessError):
            self._git_tracked = None
        return self._git_tracked

    @staticmethod
    def _age_days(path: Path, now: float) -> float:
        try:
            return max(
                0.0,
                (now - path.stat(follow_symlinks=False).st_mtime)
                / SECONDS_PER_DAY,
            )
        except OSError:
            return 0.0

    def _can_sweep_empty(self, relative: Path) -> bool:
        if not relative.parts:
            return False
        for excluded in EXCLUDED_DIRECTORY_NAMES:
            if excluded in relative.parts:
                return False
        for protected in PROTECTED_RELATIVE_DIRECTORIES:
            protected_path = Path(protected)
            if (
                relative == protected_path
                or protected_path in relative.parents
                or relative in protected_path.parents
            ):
                return False
        if relative.as_posix() in EMPTY_SWEEP_ANCHORS:
            return False
        return True

    def _emit_cleanup(self) -> LocalCleanupResult:
        started_at = _iso_now()
        now = time.time()
        cleaned_files: list[str] = []
        cleaned_directories: list[str] = []
        skipped: list[dict[str, str]] = []
        cleaned_bytes = 0
        error_count = 0
        git_tracked = self._git_tracked_paths()
        from .tool_local_cleanup_helpers import (
            _cleanup_matching_directories,
            _cleanup_matching_files,
            _sweep_empty_directories,
        )

        for walk_root, directory_names, file_names in os.walk(
            self.tool_root,
            topdown=True,
            followlinks=False,
        ):
            current_raw = Path(walk_root)
            try:
                current_raw.relative_to(self.tool_root)
            except ValueError:
                continue
            relative = current_raw.relative_to(self.tool_root)
            if self._is_protected(relative):
                directory_names[:] = []
                continue
            kept = _cleanup_matching_directories(
                self.tool_root, walk_root, directory_names, now,
                git_tracked, self._age_days, self._is_protected,
                cleaned_directories, skipped,
                [cleaned_bytes], [error_count],
                byte_quota=self.max_cleaned_bytes,
            )
            directory_names[:] = kept
            _cleanup_matching_files(
                self.tool_root, walk_root, file_names, now,
                git_tracked, self._age_days,
                cleaned_files, skipped,
                [cleaned_bytes], [error_count],
                byte_quota=self.max_cleaned_bytes,
            )
        
        _sweep_empty_directories(
            self.tool_root, self._can_sweep_empty,
            cleaned_directories, skipped, [error_count],
        )

        # Dead-code detection rides the same daily cycle — report-only:
        # candidates go to the persisted cleanup state for governed review;
        # nothing here ever deletes source files.
        dead_code_scan: dict[str, Any] | None = None
        try:
            from .tool_dead_code_scan import scan_dead_code

            dead_code_scan = scan_dead_code(self.tool_root)
        except (ImportError, OSError, ValueError, RuntimeError) as error:
            dead_code_scan = {
                "ok": False,
                "operation": "dead-code-scan",
                "error": f"{type(error).__name__}: {error}",
                "candidates": [],
            }

        return LocalCleanupResult(
            ok=error_count == 0,
            operation="local-self-cleanup",
            authority="tool-local",
            version=CLEANUP_VERSION,
            tool_id=self.tool_id,
            cleaned_files=cleaned_files,
            cleaned_directories=cleaned_directories,
            skipped=skipped,
            cleaned_bytes=cleaned_bytes,
            started_at=started_at,
            completed_at=_iso_now(),
            dead_code_scan=dead_code_scan,
        )


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                total += (Path(root) / name).stat(follow_symlinks=False).st_size
            except OSError:
                continue
    return total


def run_local_cleanup(
    tool_id: str,
    tool_root: Path | str,
    *,
    max_cleaned_bytes: int | None = None,
) -> dict[str, Any]:
    return ToolLocalCleanup(
        tool_id, tool_root, max_cleaned_bytes=max_cleaned_bytes
    )._emit_cleanup().as_dict()


__all__ = [
    "LOCAL_CLEANUP_STATE_RELATIVE_PATH",
    "LocalCleanupResult",
    "ToolLocalCleanup",
    "local_cleanup_state_path",
    "read_local_cleanup_state",
    "run_local_cleanup",
    "write_local_cleanup_state",
]
