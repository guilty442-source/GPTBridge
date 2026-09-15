# Windows background subprocess no-window flag: CREATE_NO_WINDOW.
from __future__ import annotations

import ctypes
import fnmatch
import hashlib
import hmac
import json
import math
import os
import secrets
import shutil
import stat as stat_module
import subprocess
import sys
import threading
import time
import uuid
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request

from .business_history import BusinessHistoryStore

try:
    from governance_rule.execution.git_tiers import audit_log, enforce
except Exception:
    audit_log = None
    enforce = None

from .cleanup_helpers import SECONDS_PER_DAY, LEGACY_QUARANTINE_ROOT_NAME, LEGACY_RECOVERY_ROOT_NAME, QUARANTINE_RELATIVE_PATH, RECOVERY_RELATIVE_PATH, CLEANER_RUNTIME_RELATIVE_PATH, DEFAULT_QUARANTINE_TTL_HOURS, DEFAULT_PLAN_TTL_MINUTES, MAX_REPORTED_SKIPS, MAX_HISTORY_RECORDS, PROGRESS_JSON_PREFIX, MANAGED_BACKUP_SCHEMA_VERSION, MANAGED_BACKUP_RETENTION_PER_OWNER, MANAGED_BACKUP_RELATIVE_ROOT, BACKUP_EXTRACT_RELATIVE_ROOT, SYSTEM_RESCUE_REQUIRED_PATHS, SYSTEM_RESCUE_REPAIR_ANOMALIES, PLAN_SCHEMA_VERSION, QUARANTINE_SCHEMA_VERSION, CORE_EXCLUDED_DIRECTORY_NAMES, CORE_PROTECTED_RELATIVE_PATHS, SOURCE_LIKE_SUFFIXES, _PROCESS_LOCKS_GUARD, _PROCESS_LOCKS, FALLBACK_RULES, ProgressCallback, _background_subprocess_kwargs, _deep_merge, _parse_iso


class CleanupLegacyMixin:

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
            candidates = self._legacy_purge_candidates()

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

            legacy_root_names = {
                "backups",
                "release",
                "tmp",
                "test-results",
                ".pytest_cache",
                LEGACY_QUARANTINE_ROOT_NAME,
                LEGACY_RECOVERY_ROOT_NAME,
            }

            removed: list[str] = []
            skipped: list[dict[str, str]] = []
            removed_bytes = 0
            for target in ordered:
                removed_bytes += self._purge_legacy_target(
                    target, legacy_root_names, removed, skipped
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

        def _remove_legacy_tree(
            self,
            target: Path,
            relative: str,
            *,
            honor_protection: bool,
            removed: list[str],
            skipped: list[dict[str, str]],
        ) -> int:
            """Recursively remove *target*, honoring protected descendants.

            Legacy root directories declared in ``purge_legacy_artifacts`` bypass
            the general protection list so that explicitly obsolete roots such as
            ``release`` or ``backups`` can be purged. All other directories,
            including ``platform_tools/<tool>/build``, keep their protected children
            (e.g. ``package-*`` release folders) intact.
            """

            if self._is_link_or_reparse_point(target):
                skipped.append({"path": relative, "reason": "link or reparse point"})
                return 0
            if honor_protection and self._is_protected(target):
                skipped.append({"path": relative, "reason": "protected path"})
                return 0
            if target.is_dir():
                removed_bytes = 0
                try:
                    children = sorted(target.iterdir())
                except OSError as exc:
                    skipped.append({"path": relative, "reason": f"{type(exc).__name__}: {exc}"})
                    return 0
                for child in children:
                    child_relative = self._relative_path(child)
                    removed_bytes += self._remove_legacy_tree(
                        child,
                        child_relative,
                        honor_protection=honor_protection,
                        removed=removed,
                        skipped=skipped,
                    )
                try:
                    if not any(target.iterdir()):
                        os.chmod(target, stat_module.S_IWRITE)
                        target.rmdir()
                        removed.append(relative)
                except OSError:
                    # Directory still contains protected descendants or cannot be removed.
                    pass
                return removed_bytes
            return self._remove_legacy_file(target, relative, removed, skipped)

        def _legacy_purge_candidates(self) -> set[Path]:
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
            return candidates

        def _purge_legacy_target(
            self,
            target: Path,
            legacy_root_names: set[str],
            removed: list[str],
            skipped: list[dict[str, str]],
        ) -> int:
            try:
                if (
                    target == self.project_root
                    or not target.exists()
                    or self._is_link_or_reparse_point(target)
                ):
                    return 0
                relative = self._relative_path(target)
                item_type = "directory" if target.is_dir() else "file"
                protection = self._git_protection_reason(target, item_type)
                if protection == "directory contains git-tracked files":
                    tracked, _status = self._git_snapshot()
                    prefix = relative.rstrip("/") + "/"
                    if not any(
                        item.startswith(prefix)
                        and (self.project_root / Path(item)).exists()
                        for item in tracked
                    ):
                        protection = ""
                if protection:
                    skipped.append({"path": relative, "reason": protection})
                    return 0
                honor_protection = Path(relative).name not in legacy_root_names
                return self._remove_legacy_tree(
                    target,
                    relative,
                    honor_protection=honor_protection,
                    removed=removed,
                    skipped=skipped,
                )
            except (OSError, ValueError) as error:
                skipped.append(
                    {"path": str(target), "reason": f"{type(error).__name__}: {error}"}
                )
                return 0

        @staticmethod
        def _remove_legacy_file(
            target: Path,
            relative: str,
            removed: list[str],
            skipped: list[dict[str, str]],
        ) -> int:
            try:
                size = target.stat(follow_symlinks=False).st_size
                target.unlink()
                removed.append(relative)
                return size
            except PermissionError:
                try:
                    os.chmod(target, stat_module.S_IWRITE)
                    target.unlink()
                    removed.append(relative)
                    return size
                except OSError as exc:
                    skipped.append({"path": relative, "reason": f"PermissionError: {exc}"})
                    return 0
            except OSError as exc:
                skipped.append({"path": relative, "reason": f"{type(exc).__name__}: {exc}"})
                return 0
