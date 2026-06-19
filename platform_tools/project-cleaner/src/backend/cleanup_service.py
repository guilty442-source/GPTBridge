from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any


class ProjectCleanupService:
    """Project-cleaner backend implementation.

    The mother app calls this service, but cleanup rules live with the
    project-cleaner tool so the core stays thin.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root.resolve()

    @staticmethod
    def _directory_size_bytes(paths: list[Path]) -> int:
        total = 0
        for root in paths:
            if not root.exists():
                continue
            if root.is_file():
                try:
                    total += root.stat().st_size
                except OSError:
                    pass
                continue
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                try:
                    total += path.stat().st_size
                except OSError:
                    continue
        return total

    def cleanup_garbage(self, scope: str, dry_run: bool = False) -> dict[str, Any]:
        cleaned_files = 0
        cleaned_dirs = 0
        cleaned_bytes = 0

        requested_scope = str(scope or "global").strip().lower()
        normalized_scope = (
            "global"
            if requested_scope in {"", "project", "global", "all", "full_project"}
            else requested_scope
        )

        runtime_root = self.project_root / "runtime"
        sandbox_root = self.project_root / ".GPTBridge_RuntimeSandbox"

        if normalized_scope == "sandbox":
            candidate_roots: list[Path] = [sandbox_root]
        elif normalized_scope == "runtime":
            candidate_roots = [runtime_root]
        elif normalized_scope == "global":
            candidate_roots = [self.project_root]
        else:
            return {
                "ok": False,
                "scope": requested_scope,
                "dry_run": dry_run,
                "cleaned_files": 0,
                "cleaned_dirs": 0,
                "cleaned_bytes": 0,
                "message": f"unsupported cleanup scope: {requested_scope}",
            }

        excluded_roots: list[Path] = [
            runtime_root / "profiles",
            self.project_root / "browser-profile",
            self.project_root / "edge-profile",
        ]
        excluded_dir_names = {
            ".git",
            ".venv",
            "node_modules",
            "browser-profile",
            "edge-profile",
        }

        def is_excluded(path: Path) -> bool:
            for excluded in excluded_roots:
                try:
                    path.resolve().relative_to(excluded.resolve())
                    return True
                except ValueError:
                    continue
            return False

        def is_inside_project(path: Path) -> bool:
            try:
                path.resolve().relative_to(self.project_root)
                return True
            except ValueError:
                return False

        removable_names = {
            "cache",
            "temp",
            "tmp",
            "__pycache__",
            ".pytest_cache",
            ".mypy_cache",
            ".ruff_cache",
            "logs",
        }
        removable_suffixes = {".tmp", ".old", ".log", ".bak"}

        for raw_root in candidate_roots:
            root = raw_root.resolve()
            if not root.exists():
                continue
            if not is_inside_project(root):
                continue
            if is_excluded(root):
                continue

            for current_raw, dirnames, filenames in os.walk(root):
                current = Path(current_raw)
                if is_excluded(current):
                    dirnames[:] = []
                    continue

                kept_dirnames: list[str] = []
                for dirname in dirnames:
                    child = current / dirname
                    lowered = dirname.lower()
                    if lowered in excluded_dir_names or is_excluded(child):
                        continue
                    if lowered in removable_names:
                        try:
                            size = self._directory_size_bytes([child])
                            if not dry_run:
                                shutil.rmtree(child, ignore_errors=True)
                            cleaned_dirs += 1
                            cleaned_bytes += size
                        except OSError:
                            continue
                        continue
                    kept_dirnames.append(dirname)
                dirnames[:] = kept_dirnames

                for filename in filenames:
                    path = current / filename
                    if is_excluded(path):
                        continue
                    try:
                        if path.suffix.lower() not in removable_suffixes and path.name not in {
                            ".DS_Store",
                        }:
                            continue
                        size = path.stat().st_size
                        if not dry_run:
                            path.unlink(missing_ok=True)
                        cleaned_files += 1
                        cleaned_bytes += size
                    except OSError:
                        continue

        return {
            "ok": True,
            "scope": normalized_scope,
            "requested_scope": requested_scope,
            "dry_run": dry_run,
            "cleaned_files": cleaned_files,
            "cleaned_dirs": cleaned_dirs,
            "cleaned_bytes": cleaned_bytes,
            "message": (
                f"{normalized_scope} cleanup dry-run completed (files={cleaned_files}, dirs={cleaned_dirs})"
                if dry_run
                else f"{normalized_scope} cleanup completed (files={cleaned_files}, dirs={cleaned_dirs})"
            ),
        }
