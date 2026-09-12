from __future__ import annotations

import os
import shutil
import stat as stat_module
from pathlib import Path
from typing import Any, Callable

from .cleanup_constants import (
    LEGACY_QUARANTINE_ROOT_NAME,
    LEGACY_RECOVERY_ROOT_NAME,
)


class MaintenanceMixin:
    """Legacy artifact purge — explicit, force-confirmed maintenance operation."""

    def purge_legacy_artifacts(self, *, force: bool = False) -> dict[str, Any]:
        """Delete only declared obsolete package and root-level runtime artifacts.

        This explicit maintenance operation is intentionally separate from
        ordinary cleanup policy. It never follows links, never crosses the
        project root, never removes a registered tool's current ``dist``, and
        refuses Git-tracked content. ``force`` is required because deletion is
        permanent and is used only for an explicitly authorized refactor.
        """

        if not force:
            return {
                "ok": False,
                "error_code": "CONFIRMATION_REQUIRED",
                "message": "force confirmation is required",
            }
        with self._mutation_guard("legacy-artifact-purge") as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": bool(lock.get("busy")),
                    "error_code": "CLEANER_BUSY",
                    "message": str(
                        lock.get("message") or "project cleaner is busy"
                    ),
                }
            return self._purge_legacy_artifacts_unlocked()

    def _purge_legacy_artifacts_unlocked(self) -> dict[str, Any]:
        candidates: set[Path] = set()
        for name in (
            "backups",
            "release",
            "tmp",
            "test-results",
            ".pytest_cache",
            LEGACY_QUARANTINE_ROOT_NAME,
            LEGACY_RECOVERY_ROOT_NAME,
        ):
            candidates.add(self.project_root / name)

        tools_root = self.project_root / "platform_tools"
        if tools_root.is_dir() and not self._is_link_or_reparse_point(tools_root):
            for tool_dir in tools_root.iterdir():
                if not tool_dir.is_dir() or self._is_link_or_reparse_point(tool_dir):
                    continue
                if not (tool_dir / "manifest.json").is_file():
                    candidates.add(tool_dir)
                else:
                    candidates.add(tool_dir / "build")

        # Keep only outermost candidates so a parent deletion owns its nested
        # backups and no child is evaluated after its parent is gone.
        ordered: list[Path] = []
        for candidate in sorted(candidates, key=lambda item: len(item.parts)):
            resolved = self._safe_resolve(candidate)
            if any(
                resolved == parent or parent in resolved.parents
                for parent in ordered
            ):
                continue
            ordered.append(resolved)

        removed: list[str] = []
        skipped: list[dict[str, str]] = []
        removed_bytes = 0
        for target in ordered:
            try:
                target.relative_to(self.project_root)
                if (
                    target == self.project_root
                    or not target.exists()
                    or self._is_link_or_reparse_point(target)
                ):
                    continue
                item_type = "directory" if target.is_dir() else "file"
                protection = self._git_protection_reason(target, item_type)
                if protection == "directory contains git-tracked files":
                    tracked, _status = self._git_snapshot()
                    prefix = self._relative_path(target).rstrip("/") + "/"
                    if not any(
                        item.startswith(prefix)
                        and (self.project_root / Path(item)).exists()
                        for item in tracked
                    ):
                        protection = ""
                if protection:
                    skipped.append(
                        {"path": self._relative_path(target), "reason": protection}
                    )
                    continue
                snapshot = self._candidate_snapshot(target, item_type)
                removed_bytes += int(snapshot.get("size_bytes") or 0)
                relative = self._relative_path(target)
                if item_type == "directory":
                    def clear_readonly_and_retry(
                        operation: Callable[..., Any],
                        path: str,
                        _error: tuple[type[BaseException], BaseException, Any],
                    ) -> None:
                        os.chmod(path, stat_module.S_IWRITE)
                        operation(path)

                    shutil.rmtree(target, onerror=clear_readonly_and_retry)
                else:
                    target.unlink()
                removed.append(relative)
            except (OSError, ValueError) as error:
                skipped.append(
                    {"path": str(target), "reason": f"{type(error).__name__}: {error}"}
                )
        return {
            "ok": not skipped,
            "authority": "global-cleaner",
            "boundary": "project-only",
            "removed": removed,
            "removed_count": len(removed),
            "removed_bytes": removed_bytes,
            "skipped": skipped,
            "permanently_deleted": True,
            "message": f"legacy artifacts purged (removed={len(removed)})",
        }
