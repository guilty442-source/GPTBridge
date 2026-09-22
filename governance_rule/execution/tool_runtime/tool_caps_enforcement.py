"""§10.67 E — per-module caps enforcement (governor directives 2026-09-22).

Bounds each module's private data/artifacts/cache/logs:

- **per-module cap** (default 10 MB): when a module exceeds it, eligible
  files are pruned oldest-first until under cap * headroom (0.9);
- **72 h age rule**: unprotected artifacts (logs, reports, governed job
  dirs, snapshots, staging/candidate, temp files) older than the policy
  age are deleted — the cap OR the age, whichever trips first;
- **backups keep one**: inside any ``backups`` directory only the newest
  entry survives;
- **snapshots used-and-deleted**: ``*.staging-tmp``, ``runtime/temp``
  contents and empty staging/candidate dirs are collected immediately
  (the 1 h minimum age guards against mid-write races);
- **fail-closed keep**: files referenced by any ``lifecycle.json`` /
  ``native-engine.json`` / ``*.pin`` under the module root, files inside
  protected directories, and git-tracked paths are never removed.

Policy: ``main-system/config/cleanup-caps-policy.json`` (changes are
audited); every deletion is recorded in the result for the caller's
audit ledger.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

SECONDS_PER_HOUR = 3600.0

PROTECTED_RELATIVE_DIRECTORIES = (
    "runtime/ipc",
    "runtime/state",
    "runtime/recovery",
    "runtime/profiles",
    "data/business",
)

EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "dist-ui",
        "release",
        "electron-user-data",
    }
)

# categories eligible for the 72h age rule and cap pruning
_AGE_ELIGIBLE_NAMES = frozenset(
    {
        "logs", "reports", "jobs", "snapshots", "staging", "candidates",
        "temp", "tmp", "checkpoints-scratch", "outbox-scratch",
    }
)
_AGE_ELIGIBLE_SUFFIXES = (".log", ".tmp", ".temp", ".staging-tmp", ".bak")
_BACKUP_DIR_NAMES = frozenset({"backups", "backup"})
_REFERENCE_FILES = ("lifecycle.json", "native-engine.json", "retention.json")


def _is_protected(relative: Path) -> bool:
    for protected in PROTECTED_RELATIVE_DIRECTORIES:
        protected_path = Path(protected)
        if relative == protected_path or protected_path in relative.parents:
            return True
    for excluded in EXCLUDED_DIRECTORY_NAMES:
        if excluded in relative.parts:
            return True
    return False


def _referenced_paths(tool_root: Path) -> frozenset[str]:
    """Paths referenced by lifecycle/pin manifests — fail-closed keep."""
    referenced: set[str] = set()
    for name in _REFERENCE_FILES:
        for manifest in tool_root.rglob(name):
            try:
                payload = json.loads(manifest.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                # unresolvable manifest -> fail-closed: keep everything nearby
                try:
                    rel_dir = manifest.parent.relative_to(
                        tool_root.resolve()
                    ).as_posix()
                    # "." (module root) must prefix-match everything
                    referenced.add("" if rel_dir == "." else rel_dir)
                except ValueError:
                    referenced.add(
                        str(manifest.parent).replace("\\", "/")
                    )
                continue
            for match in re.finditer(
                r'"[^"]*(?:path|checkpoint|artifact|weights|root)[^"]*"\s*:\s*"([^"]+)"',
                json.dumps(payload),
            ):
                candidate = (manifest.parent / match.group(1)).resolve()
                try:
                    referenced.add(
                        candidate.relative_to(tool_root.resolve())
                        .as_posix()
                    )
                except ValueError:
                    referenced.add(str(candidate).replace("\\", "/"))
    return frozenset(referenced)


def _git_tracked(tool_root: Path) -> frozenset[str] | None:
    if not (tool_root / ".git").exists() and not any(
        (p / ".git").exists() for p in tool_root.parents
    ):
        return frozenset()
    try:
        completed = subprocess.run(
            ["git", "-C", str(tool_root), "ls-files", "-z"],
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            return None
        prefix = subprocess.run(
            ["git", "-C", str(tool_root), "rev-parse", "--show-prefix"],
            capture_output=True,
            timeout=10,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if prefix.returncode != 0:
            return None
        prefix_text = prefix.stdout.decode("utf-8", "replace").strip()
        return frozenset(
            item.replace("\\", "/")[len(prefix_text):]
            for item in completed.stdout.decode(
                "utf-8", errors="surrogateescape"
            ).split("\0")
            if item and item.replace("\\", "/").startswith(prefix_text)
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _remove(path: Path) -> None:
    try:
        os.chmod(path, 0o666)
    except OSError:
        pass
    path.unlink()


def run_caps_enforcement(
    tool_id: str,
    tool_root: Path | str,
    *,
    cap_bytes: int = 10 * 1024 * 1024,
    max_age_hours: float = 72.0,
    headroom: float = 0.9,
    backup_keep: int = 1,
) -> dict[str, Any]:
    """Enforce §10.67 E caps inside one module root. Returns an audit record."""
    started = time.time()
    root = Path(tool_root).resolve()
    result: dict[str, Any] = {
        "tool_id": tool_id,
        "tool_root": str(root),
        "cap_bytes": cap_bytes,
        "max_age_hours": max_age_hours,
        "removed": [],
        "kept_referenced": 0,
        "ok": True,
        "error": "",
    }
    if not root.is_dir():
        result["ok"] = False
        result["error"] = "MODULE_ROOT_MISSING"
        return result

    referenced = _referenced_paths(root)
    tracked = _git_tracked(root)
    if tracked is None:
        # cannot prove tracked status -> fail-closed, nothing removed
        result["ok"] = False
        result["error"] = "GIT_TRACKING_UNKNOWN"
        return result

    now = time.time()
    removed_bytes = 0

    def _removable(relative: Path, path: Path) -> str | None:
        """Return a removal reason or None when protected."""
        if _is_protected(relative):
            return None
        rel_posix = relative.as_posix()
        if rel_posix in tracked:
            return None
        if any(rel_posix.startswith(r) or r.startswith(rel_posix)
               for r in referenced):
            return None
        return "eligible"

    def _record(path: Path, relative: Path, reason: str) -> None:
        nonlocal removed_bytes
        try:
            size = path.stat(follow_symlinks=False).st_size
        except OSError:
            size = 0
        try:
            _remove(path)
            removed_bytes += size
            result["removed"].append(
                {"path": relative.as_posix(), "bytes": size, "reason": reason}
            )
        except OSError as error:
            result["ok"] = False
            result["error"] = f"remove failed: {relative}: {error}"

    # --- pass 1: backups keep-latest-N --------------------------------
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        relative_dir = base.relative_to(root)
        if _is_protected(relative_dir):
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if not _is_protected(relative_dir / d)
        ]
        if base.name.lower() not in _BACKUP_DIR_NAMES:
            continue
        candidates = []
        for name in filenames:
            path = base / name
            if _removable(relative_dir / name, path) != "eligible":
                continue
            try:
                candidates.append(
                    (path.stat(follow_symlinks=False).st_mtime, path)
                )
            except OSError:
                continue
        candidates.sort(reverse=True)  # newest first
        for _mtime, path in candidates[backup_keep:]:
            _record(path, path.relative_to(root), "backup-keep-latest")

    # --- pass 2: age rule (72h) on eligible categories -----------------
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        relative_dir = base.relative_to(root)
        if _is_protected(relative_dir):
            dirnames[:] = []
            continue
        dirnames[:] = [
            d for d in dirnames
            if not _is_protected(relative_dir / d)
        ]
        age_eligible_dir = bool(
            set(relative_dir.parts) & _AGE_ELIGIBLE_NAMES
        )
        for name in filenames:
            path = base / name
            relative = relative_dir / name
            suffix = path.suffix.lower()
            staging_tmp = name.endswith(".staging-tmp")
            if _removable(relative, path) != "eligible":
                continue
            try:
                age_h = (now - path.stat(follow_symlinks=False).st_mtime) / SECONDS_PER_HOUR
            except OSError:
                continue
            if staging_tmp or suffix in {".tmp", ".temp"}:
                if age_h >= 1.0:  # mid-write race guard
                    _record(path, relative, "staging-temp")
            elif age_h >= max_age_hours and (
                age_eligible_dir or suffix in _AGE_ELIGIBLE_SUFFIXES
            ):
                _record(path, relative, "age-72h")

    # --- pass 3: cap enforcement (oldest eligible first) ---------------
    def _module_size() -> int:
        total = 0
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            base = Path(dirpath)
            relative_dir = base.relative_to(root)
            if _is_protected(relative_dir):
                dirnames[:] = []
                continue
            dirnames[:] = [
                d for d in dirnames
                if not _is_protected(relative_dir / d)
            ]
            for name in filenames:
                try:
                    total += (base / name).stat(follow_symlinks=False).st_size
                except OSError:
                    continue
        return total

    size = _module_size()
    if size > cap_bytes:
        eligible: list[tuple[float, int, Path, Path]] = []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            base = Path(dirpath)
            relative_dir = base.relative_to(root)
            if _is_protected(relative_dir):
                dirnames[:] = []
                continue
            dirnames[:] = [
                d for d in dirnames
                if not _is_protected(relative_dir / d)
            ]
            for name in filenames:
                path = base / name
                relative = relative_dir / name
                if _removable(relative, path) != "eligible":
                    continue
                # cap pruning stays inside cleanup-eligible surfaces:
                # caches, logs, temp, staging, snapshots — never source,
                # config, manifests or business data.
                parts = set(relative_dir.parts)
                if not (
                    parts & _AGE_ELIGIBLE_NAMES
                    or path.suffix.lower() in _AGE_ELIGIBLE_SUFFIXES
                    or "__pycache__" in parts
                ):
                    continue
                try:
                    stat = path.stat(follow_symlinks=False)
                    eligible.append((stat.st_mtime, stat.st_size, path, relative))
                except OSError:
                    continue
        eligible.sort()  # oldest first
        target = int(cap_bytes * headroom)
        for _mtime, fsize, path, relative in eligible:
            if size <= target:
                break
            _record(path, relative, "cap-overflow")
            size -= fsize

    result["module_bytes_after"] = _module_size()
    result["removed_bytes"] = removed_bytes
    result["removed_count"] = len(result["removed"])
    result["elapsed_ms"] = round((time.time() - started) * 1000, 1)
    return result


__all__ = ["run_caps_enforcement"]
