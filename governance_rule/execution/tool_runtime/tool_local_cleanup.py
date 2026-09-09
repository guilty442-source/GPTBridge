from __future__ import annotations

import json
import os
import shutil
import stat as stat_module
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

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self)}


class ToolLocalCleanup:
    """Bounded, tool-root-local low-risk garbage cleanup.

    Every candidate is forced to resolve within ``tool_root`` and is rejected
    unless it is one of the explicitly declared safe patterns below. It never
    touches other modules, ejects protected directories, follows links, or
    removes git-tracked content.
    """

    def __init__(self, tool_id: str, tool_root: Path | str) -> None:
        self.tool_id = tool_id
        self.tool_root = Path(tool_root).resolve()

    def _is_protected(self, relative: Path) -> bool:
        for protected in PROTECTED_RELATIVE_DIRECTORIES:
            if relative == Path(protected) or protected.startswith(
                f"{relative.as_posix()}/"
            ):
                return False
        for excluded in EXCLUDED_DIRECTORY_NAMES:
            if excluded in relative.parts:
                return True
        return False

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

            kept_directories: list[str] = []
            for name in directory_names:
                candidate = current_raw / name
                relative_dir = (current_raw / name).relative_to(self.tool_root)
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
                    if not _inside(candidate, self.tool_root):
                        continue
                    try:
                        if matched_rule.get("contents_only"):
                            _emit_contents(candidate, self.tool_root)
                            cleaned_directories.append(
                                relative_dir.as_posix()
                            )
                            continue
                        cleaned_bytes += _dir_size(candidate)
                        _remove_path(candidate)
                        cleaned_directories.append(relative_dir.as_posix())
                    except OSError as error:
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
            directory_names[:] = kept_directories

            for name in file_names:
                candidate = current_raw / name
                if not _inside(candidate, self.tool_root):
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
                try:
                    age_days = max(
                        0.0,
                        (now - candidate.stat(follow_symlinks=False).st_mtime)
                        / SECONDS_PER_DAY,
                    )
                except OSError:
                    continue
                if age_days < float(rule["min_age_days"]):
                    continue
                try:
                    cleaned_bytes += candidate.stat(follow_symlinks=False).st_size
                    _remove_path(candidate)
                    cleaned_files.append(
                        candidate.relative_to(self.tool_root).as_posix()
                    )
                except OSError as error:
                    skipped.append(
                        {
                            "path": candidate.relative_to(self.tool_root).as_posix(),
                            "reason": f"{type(error).__name__}: {error}",
                        }
                    )

        for walk_root, directory_names, file_names in os.walk(
            self.tool_root,
            topdown=False,
            followlinks=False,
        ):
            current_raw = Path(walk_root)
            try:
                relative = current_raw.relative_to(self.tool_root)
            except ValueError:
                continue
            if current_raw == self.tool_root or not self._can_sweep_empty(relative):
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
                skipped.append(
                    {
                        "path": relative.as_posix(),
                        "reason": f"{type(error).__name__}: {error}",
                    }
                )

        return LocalCleanupResult(
            ok=not skipped,
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


def run_local_cleanup(tool_id: str, tool_root: Path | str) -> dict[str, Any]:
    return ToolLocalCleanup(tool_id, tool_root)._emit_cleanup().as_dict()


__all__ = [
    "LOCAL_CLEANUP_STATE_RELATIVE_PATH",
    "LocalCleanupResult",
    "ToolLocalCleanup",
    "local_cleanup_state_path",
    "read_local_cleanup_state",
    "run_local_cleanup",
    "write_local_cleanup_state",
]
