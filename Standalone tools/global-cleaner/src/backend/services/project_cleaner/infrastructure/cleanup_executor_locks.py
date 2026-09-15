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


class CleanupLocksMixin:

        def run_governed_daily_maintenance(self) -> dict[str, Any]:
            requester = str(
                os.environ.get("GPTBRIDGE_GOVERNED_REQUESTER_ACTOR") or ""
            ).strip()
            if requester != "governance/main-system":
                raise PermissionError("PERMISSION_DENIED")
            # Cross-module low-risk garbage cleanup has been devolved to each
            # independent tool's own local self-cleanup at boot. The central daily
            # maintenance now only owns governed backups, which remain the single
            # backup authority that recovery/system-rescue depend on.
            backups: list[dict[str, Any]] = []
            for owner_id in sorted(self._registered_backup_owners()):
                self._emit_progress(
                    "backup",
                    0,
                    "建立治理備份",
                    current_path=owner_id,
                )
                backups.append(self.create_managed_backup(owner_id))
            failed_backups = [item for item in backups if item.get("ok") is not True]
            cleanup = {
                "ok": True,
                "operation": "local-self-cleanup-devolved",
                "cleaned_files": 0,
                "cleaned_dirs": 0,
                "cleaned_bytes": 0,
                "message": "garbage cleanup devolved to per-tool local self-cleanup",
            }
            result = {
                "ok": not failed_backups,
                "operation": "governed-daily-maintenance",
                "authority": "governance/main-system -> shared-layer -> global-cleaner",
                "cleanup": cleanup,
                "backups": backups,
                "backup_owner_count": len(backups),
                "backup_failure_count": len(failed_backups),
                "message": "daily governed backup completed; garbage cleanup devolved to per-tool self-cleanup",
            }
            self._append_history(
                "daily-maintenance",
                ok=result["ok"],
                scope="global",
                item_count=0,
                bytes=0,
                errors=len(failed_backups),
            )
            return result

        def _harden_private_path(self, path: Path) -> None:
            """Best-effort owner-only permissions for plans, keys, and quarantine."""

            resolved = self._safe_resolve(path)
            key = os.path.normcase(str(resolved))
            if key in self._hardened_private_paths or not resolved.exists():
                return
            try:
                resolved.chmod(0o700 if resolved.is_dir() else 0o600)
            except OSError:
                pass
            if os.name == "nt":
                user_name = os.environ.get("USERNAME", "").strip()
                if user_name:
                    owner_grant = (
                        f"{user_name}:(OI)(CI)F" if resolved.is_dir() else f"{user_name}:F"
                    )
                    system_grant = "SYSTEM:(OI)(CI)F" if resolved.is_dir() else "SYSTEM:F"
                    try:
                        subprocess.run(
                            [
                                "icacls",
                                str(resolved),
                                "/inheritance:r",
                                "/grant:r",
                                owner_grant,
                                system_grant,
                            ],
                            check=False,
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                            timeout=10,
                            **_background_subprocess_kwargs(),
                        )
                    except (OSError, subprocess.SubprocessError):
                        pass
            self._hardened_private_paths.add(key)

        def _operation_lock_path(self, resource: str) -> Path:
            """Return a contained, non-user-controlled path for an operation lock."""

            runtime_root = self._safe_resolve(self.runtime_root)
            lock_root = self._safe_resolve(runtime_root / "locks")
            try:
                lock_root.relative_to(runtime_root)
            except ValueError as exc:
                raise ValueError("operation lock directory escaped cleaner runtime") from exc
            digest = hashlib.sha256(str(resource).encode("utf-8", errors="replace")).hexdigest()
            lock_path = self._safe_resolve(lock_root / f"{digest}.lock")
            try:
                lock_path.relative_to(lock_root)
            except ValueError as exc:  # Defensive: the digest filename should make this impossible.
                raise ValueError("operation lock escaped the private lock directory") from exc
            return lock_path

        @staticmethod
        def _process_lock_for(path: Path) -> threading.Lock:
            key = os.path.normcase(str(path))
            with _PROCESS_LOCKS_GUARD:
                lock = _PROCESS_LOCKS.get(key)
                if lock is None:
                    lock = threading.Lock()
                    _PROCESS_LOCKS[key] = lock
                return lock

        @staticmethod
        def _try_os_file_lock(handle: BinaryIO) -> tuple[bool, str]:
            """Acquire a one-byte non-blocking lock without PID-based stale cleanup.

            The lock file deliberately remains in place. Kernel locks are released
            automatically when a process exits, so a stale PID is never used as a
            reason to unlink a lock still owned by another process.
            """

            try:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True, ""
            except (OSError, ImportError) as exc:
                return False, str(exc)

        @staticmethod
        def _release_os_file_lock(handle: BinaryIO) -> None:
            try:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except (OSError, ImportError):
                pass

        @contextmanager
        def _exclusive_resource_lock(
            self,
            resource: str,
            *,
            operation: str,
        ) -> Iterator[dict[str, Any]]:
            try:
                lock_path = self._operation_lock_path(resource)
            except ValueError as exc:
                yield {
                    "acquired": False,
                    "busy": False,
                    "message": f"project cleaner mutation lock is unsafe: {exc}",
                }
                return
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            self._harden_private_path(lock_path.parent)
            process_lock = self._process_lock_for(lock_path)
            if not process_lock.acquire(blocking=False):
                yield {
                    "acquired": False,
                    "busy": True,
                    "message": "another project cleaner mutation is already running",
                }
                return

            handle: BinaryIO | None = None
            os_locked = False
            acquire_error = ""
            try:
                try:
                    lock_path.touch(exist_ok=True)
                    handle = lock_path.open("r+b")
                    os_locked, acquire_error = self._try_os_file_lock(handle)
                except OSError as exc:
                    acquire_error = str(exc)
                if not os_locked or handle is None:
                    yield {
                        "acquired": False,
                        "busy": True,
                        "message": (
                            "another project cleaner mutation is already running"
                            if not acquire_error
                            else f"project cleaner mutation lock unavailable: {acquire_error}"
                        ),
                    }
                    return

                metadata = {
                    "schema_version": 1,
                    "state": "locked",
                    "operation": operation,
                    "pid": os.getpid(),
                    "acquired_at": self._iso_now(),
                }
                try:
                    handle.seek(0)
                    handle.write(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
                    handle.truncate()
                    handle.flush()
                    os.fsync(handle.fileno())
                except OSError:
                    # Lock ownership, not advisory metadata, controls exclusion.
                    pass
                yield {"acquired": True, "busy": False, "path": str(lock_path)}
            finally:
                if handle is not None:
                    if os_locked:
                        try:
                            released = {
                                "schema_version": 1,
                                "state": "released",
                                "operation": operation,
                                "pid": os.getpid(),
                                "released_at": self._iso_now(),
                            }
                            handle.seek(0)
                            handle.write(
                                json.dumps(released, ensure_ascii=False).encode("utf-8")
                            )
                            handle.truncate()
                            handle.flush()
                            os.fsync(handle.fileno())
                        except OSError:
                            pass
                        self._release_os_file_lock(handle)
                    handle.close()
                process_lock.release()

        @contextmanager
        def _mutation_guard(
            self,
            operation: str,
            *,
            batch_name: str = "",
        ) -> Iterator[dict[str, Any]]:
            resources = ["project-mutation"]
            if batch_name:
                resources.append(f"quarantine-batch:{Path(batch_name).name}")
            with ExitStack() as stack:
                for resource in resources:
                    lock = stack.enter_context(
                        self._exclusive_resource_lock(resource, operation=operation)
                    )
                    if not lock.get("acquired"):
                        yield lock
                        return
                yield {"acquired": True, "busy": False}
