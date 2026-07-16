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
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterator
from urllib import error as urllib_error
from urllib import request as urllib_request


SECONDS_PER_DAY = 24 * 60 * 60
QUARANTINE_ROOT_NAME = ".GPTBridge_CleanerQuarantine"
RECOVERY_ROOT_NAME = ".GPTBridge_CleanerRecovery"
CLEANER_DATA_RELATIVE_PATH = Path("platform_tools") / "project-cleaner" / "data"
DEFAULT_QUARANTINE_TTL_HOURS = 24
DEFAULT_PLAN_TTL_MINUTES = 15
MAX_REPORTED_SKIPS = 200
MAX_HISTORY_RECORDS = 200
PROGRESS_JSON_PREFIX = "PROJECT_CLEANER_PROGRESS_JSON="
SYSTEM_RESCUE_REQUIRED_PATHS = (
    "src-core/main.py",
    "src-core/ipc/server.py",
    "src-core/ipc/handlers.py",
    "config/settings.json",
    "config/tool-runtime-contract.json",
    "package.json",
    "scripts/package_platform_tools.py",
)
SYSTEM_RESCUE_REPAIR_ANOMALIES = frozenset(
    {
        "invalid-production-signature",
        "orphan-atomic-temp",
        "stale-package-lock",
        "stale-preview-plan",
    }
)
PLAN_SCHEMA_VERSION = 2
QUARANTINE_SCHEMA_VERSION = 2
CORE_EXCLUDED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "browser-profile",
        "browser-profiles",
        "edge-profile",
        "backups",
        QUARANTINE_ROOT_NAME,
        RECOVERY_ROOT_NAME,
    }
)
CORE_PROTECTED_RELATIVE_PATHS = frozenset(
    {
        "runtime/profiles",
        "runtime/state",
        "runtime/browser-profiles",
        "runtime/project-cleaner",
        "runtime/file-sorter",
        "runtime/ipc",
        "dist-ui",
        "release",
        "browser-profile",
        "edge-profile",
        "backups",
        "platform_tools/project-cleaner/data",
        QUARANTINE_ROOT_NAME,
        RECOVERY_ROOT_NAME,
    }
)
SOURCE_LIKE_SUFFIXES = {
    ".c",
    ".cc",
    ".cpp",
    ".cs",
    ".css",
    ".go",
    ".h",
    ".hpp",
    ".html",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".md",
    ".php",
    ".ps1",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".sql",
    ".toml",
    ".ts",
    ".tsx",
    ".vue",
    ".xml",
    ".yaml",
    ".yml",
}
ProgressCallback = Callable[[dict[str, Any]], None]


# The in-process lock closes a gap in platforms where file-lock semantics are
# process-scoped. The OS lock below is still authoritative across processes.
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[str, threading.Lock] = {}


FALLBACK_RULES: dict[str, Any] = {
    "schema_version": 2,
    "plan_ttl_minutes": DEFAULT_PLAN_TTL_MINUTES,
    "quarantine_ttl_hours": DEFAULT_QUARANTINE_TTL_HOURS,
    "excluded_directory_names": [
        ".git",
        ".venv",
        "node_modules",
        "browser-profile",
        "edge-profile",
        "backups",
        QUARANTINE_ROOT_NAME,
        RECOVERY_ROOT_NAME,
    ],
    "protected_relative_paths": [
        "runtime/profiles",
        "runtime/state",
        "runtime/project-cleaner",
        "browser-profile",
        "edge-profile",
        "backups",
        "platform_tools/project-cleaner/data",
        QUARANTINE_ROOT_NAME,
        RECOVERY_ROOT_NAME,
    ],
    "directory_rules": [
        {
            "id": "visual-smoke-sandbox",
            "patterns": ["gptbridge-ai-assistant-visual-*"],
            "reason": "isolated visual smoke sandbox",
            "risk": "low",
            "min_age_days": 0,
            "protect_user_content": False,
            "sandbox_only": True,
        },
        {
            "id": "python-bytecode",
            "names": ["__pycache__"],
            "reason": "python bytecode cache",
            "risk": "low",
            "min_age_days": 0,
            "protect_user_content": False,
        },
        {
            "id": "tool-cache",
            "names": [".pytest_cache", ".mypy_cache", ".ruff_cache"],
            "reason": "tool cache directory",
            "risk": "low",
            "min_age_days": 0,
            "protect_user_content": False,
        },
        {
            "id": "generic-cache",
            "names": ["cache", "temp", "tmp"],
            "reason": "generated working directory",
            "risk": "low",
            "min_age_days": 0,
            "protect_user_content": True,
        },
    ],
    "file_rules": [
        {
            "id": "temporary",
            "patterns": ["*.tmp", "*.temp"],
            "reason": "temporary file",
            "risk": "low",
            "min_age_days": 1,
        },
        {
            "id": "old-log",
            "patterns": ["*.log"],
            "reason": "expired log file",
            "risk": "low",
            "min_age_days": 7,
        },
        {
            "id": "old-copy",
            "patterns": ["*.old", "*.bak"],
            "reason": "old file copy",
            "risk": "medium",
            "min_age_days": 14,
        },
    ],
    "analysis": {
        "duplicate_min_size_bytes": 1024 * 1024,
        "large_file_min_size_bytes": 10 * 1024 * 1024,
        "large_file_min_age_days": 14,
        "hash_chunk_bytes": 1024 * 1024,
    },
    "automation": {
        "enabled": False,
        "scope": "global",
        "interval_hours": 24,
        "disk_free_threshold_percent": 15,
        "max_bytes_per_run": 512 * 1024 * 1024,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(dict(merged[key]), value)
        else:
            merged[key] = value
    return merged


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class ProjectCleanupService:
    VERSION = "1.0.0"

    def purge_legacy_artifacts(self, *, force: bool = False) -> dict[str, Any]:
        """Delete obsolete packages and every untracked backup inside the project.

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
        candidates: set[Path] = set()
        for name in (
            "backups",
            "release",
            "tmp",
            "test-results",
            ".pytest_cache",
            QUARANTINE_ROOT_NAME,
            RECOVERY_ROOT_NAME,
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

        excluded = {
            ".git",
            ".venv",
            "node_modules",
            "dist",
            "dist-ui",
            "browser-profile",
            "browser-profiles",
            "edge-profile",
        }
        for current_raw, dirnames, filenames in os.walk(
            self.project_root, topdown=True, followlinks=False
        ):
            current = Path(current_raw)
            dirnames[:] = [
                name
                for name in dirnames
                if name not in excluded
                and not self._is_link_or_reparse_point(current / name)
            ]
            for dirname in dirnames:
                lowered = dirname.casefold()
                if lowered in {"backup", "backups", "recovery"} or lowered.startswith(
                    ("backup-", "recovery-")
                ):
                    candidates.add(current / dirname)
            for filename in filenames:
                if Path(filename).suffix.casefold() in {".bak", ".old"}:
                    candidates.add(current / filename)

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
            "authority": "project-cleaner",
            "boundary": "project-only",
            "removed": removed,
            "removed_count": len(removed),
            "removed_bytes": removed_bytes,
            "skipped": skipped,
            "permanently_deleted": True,
            "message": f"legacy artifacts purged (removed={len(removed)})",
        }

    def __init__(
        self,
        project_root: Path,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.data_root = self.project_root / CLEANER_DATA_RELATIVE_PATH
        self.backup_root = self.data_root / "backups"
        self.audit_root = self.data_root / "audit"
        self.logs_root = self.data_root / "logs"
        self.quarantine_root = self.project_root / QUARANTINE_ROOT_NAME
        self.recovery_root = self.project_root / RECOVERY_ROOT_NAME
        self.runtime_root = self.project_root / "runtime" / "project-cleaner"
        self.plan_root = self.runtime_root / "plans"
        self.history_path = self.audit_root / "project-cleaner" / "history.jsonl"
        self.key_path = self.runtime_root / "plan.key"
        self.rules_override_path = self.project_root / "config" / "project-cleaner-rules.json"
        self.progress_callback = progress_callback
        self._hardened_private_paths: set[str] = set()
        self.rules, self.rule_warnings = self._load_rules()
        self._git_tracked_cache: set[str] | None = None
        self._git_status_cache: dict[str, Any] | None = None

    @staticmethod
    def _safe_resolve(path: Path) -> Path:
        try:
            return path.resolve()
        except OSError:
            return path.absolute()

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

    @staticmethod
    def _iso_now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _age_days(path: Path, now: float) -> float:
        try:
            return max(0.0, (now - path.stat(follow_symlinks=False).st_mtime) / SECONDS_PER_DAY)
        except OSError:
            return 0.0

    @staticmethod
    def _is_link_or_reparse_point(path: Path) -> bool:
        try:
            if path.is_symlink():
                return True
            attrs = getattr(path.stat(follow_symlinks=False), "st_file_attributes", 0)
            reparse_flag = getattr(stat_module, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            return bool(reparse_flag and attrs & reparse_flag)
        except OSError:
            return False

    def _emit_progress(self, phase: str, percent: int, message: str, **payload: Any) -> None:
        if self.progress_callback is None:
            return
        event = {
            "phase": phase,
            "percent": max(0, min(100, int(percent))),
            "message": message,
            "timestamp": self._iso_now(),
            **payload,
        }
        try:
            self.progress_callback(event)
        except Exception:
            pass

    def _load_rules(self) -> tuple[dict[str, Any], list[str]]:
        warnings: list[str] = []
        bundled_path = Path(__file__).with_name("cleanup_rules.json")
        rules = json.loads(json.dumps(FALLBACK_RULES))
        if bundled_path.exists():
            try:
                payload = json.loads(bundled_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    rules = _deep_merge(rules, payload)
            except (OSError, json.JSONDecodeError) as exc:
                warnings.append(f"bundled rule file invalid: {exc}")
        if self.rules_override_path.exists():
            try:
                payload = json.loads(self.rules_override_path.read_text(encoding="utf-8"))
                list_fields = (
                    "excluded_directory_names",
                    "protected_relative_paths",
                    "directory_rules",
                    "file_rules",
                )
                valid_override = (
                    isinstance(payload, dict)
                    and int(payload.get("schema_version") or 0) == PLAN_SCHEMA_VERSION
                    and all(
                        field not in payload or isinstance(payload.get(field), list)
                        for field in list_fields
                    )
                    and all(
                        field not in payload or isinstance(payload.get(field), dict)
                        for field in ("analysis", "automation")
                    )
                )
                if valid_override:
                    rules = _deep_merge(rules, payload)
                else:
                    warnings.append(
                        "rule override ignored: schema or field types are invalid"
                    )
            except (OSError, json.JSONDecodeError) as exc:
                warnings.append(f"rule override invalid: {exc}")
        excluded = {
            str(item)
            for item in rules.get("excluded_directory_names", [])
            if str(item).strip()
        }
        protected = {
            str(item).replace("\\", "/").strip("/")
            for item in rules.get("protected_relative_paths", [])
            if str(item).strip()
        }
        rules["excluded_directory_names"] = sorted(
            excluded | CORE_EXCLUDED_DIRECTORY_NAMES,
            key=str.casefold,
        )
        rules["protected_relative_paths"] = sorted(
            protected | CORE_PROTECTED_RELATIVE_PATHS,
            key=str.casefold,
        )
        if int(rules.get("schema_version") or 0) != PLAN_SCHEMA_VERSION:
            warnings.append("rule schema version does not match cleaner schema")
        return rules, warnings

    @staticmethod
    def _fsync_parent_directory(directory: Path) -> None:
        """Best-effort persistence barrier for a completed atomic replace."""

        if os.name == "nt":
            try:
                from ctypes import wintypes

                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                create_file = kernel32.CreateFileW
                create_file.argtypes = [
                    wintypes.LPCWSTR,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.LPVOID,
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.HANDLE,
                ]
                create_file.restype = wintypes.HANDLE
                flush_file_buffers = kernel32.FlushFileBuffers
                flush_file_buffers.argtypes = [wintypes.HANDLE]
                flush_file_buffers.restype = wintypes.BOOL
                close_handle = kernel32.CloseHandle
                close_handle.argtypes = [wintypes.HANDLE]
                close_handle.restype = wintypes.BOOL

                file_share_all = 0x00000001 | 0x00000002 | 0x00000004
                open_existing = 3
                file_flag_backup_semantics = 0x02000000
                handle = create_file(
                    str(directory),
                    0,
                    file_share_all,
                    None,
                    open_existing,
                    file_flag_backup_semantics,
                    None,
                )
                invalid_handle = ctypes.c_void_p(-1).value
                if handle not in (None, 0, invalid_handle):
                    try:
                        flush_file_buffers(handle)
                    finally:
                        close_handle(handle)
                return
            except (OSError, AttributeError, ValueError):
                return

        descriptor: int | None = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            descriptor = os.open(str(directory), flags)
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _atomic_write_json(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        serialized = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        try:
            with temp_path.open(
                "w",
                encoding="utf-8",
                newline="\n",
            ) as target:
                target.write(serialized)
                target.flush()
                os.fsync(target.fileno())
            os.replace(temp_path, path)
            self._fsync_parent_directory(path.parent)
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    def _append_history(self, action: str, **payload: Any) -> None:
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self._harden_private_path(self.history_path.parent)
        record = {
            "operation_id": uuid.uuid4().hex,
            "timestamp": self._iso_now(),
            "action": action,
            **payload,
        }
        with self.history_path.open("a", encoding="utf-8", newline="\n") as target:
            target.write(json.dumps(record, ensure_ascii=False) + "\n")
            target.flush()
            os.fsync(target.fileno())

    def _history_records(self, *, limit: int = 50) -> list[dict[str, Any]]:
        if not self.history_path.exists():
            return []
        records: list[dict[str, Any]] = []
        try:
            for line in self.history_path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    records.append(value)
        except OSError:
            return []
        return records[-max(0, limit) :]

    def _inside_project(self, path: Path) -> bool:
        try:
            self._safe_resolve(path).relative_to(self.project_root)
            return True
        except ValueError:
            return False

    def _protected_roots(self) -> list[Path]:
        configured = self.rules.get("protected_relative_paths", [])
        roots = [
            self.project_root / str(item)
            for item in configured
            if str(item or "").strip()
        ]
        roots.extend([self.runtime_root, self.quarantine_root])
        unique: dict[str, Path] = {}
        for root in roots:
            unique[str(self._safe_resolve(root)).casefold()] = root
        return list(unique.values())

    def _is_protected(self, path: Path) -> bool:
        resolved = self._safe_resolve(path)
        try:
            relative_parts = resolved.relative_to(self.project_root).parts
        except ValueError:
            return True
        if len(relative_parts) >= 3 and relative_parts[0].casefold() == "platform_tools":
            tool_area = relative_parts[2].casefold()
            if tool_area == "dist":
                return True
            if (
                tool_area == "build"
                and len(relative_parts) >= 4
                and (
                    relative_parts[3].startswith("package-")
                    or relative_parts[3].startswith(".package-")
                )
            ):
                return True
        for protected in self._protected_roots():
            try:
                resolved.relative_to(self._safe_resolve(protected))
                return True
            except ValueError:
                continue
        return False

    def _candidate_roots(self, requested_scope: str) -> tuple[str, list[Path] | None]:
        normalized_scope = (
            "global"
            if requested_scope in {"", "project", "global", "all", "full_project"}
            else requested_scope
        )
        if normalized_scope == "sandbox":
            return normalized_scope, [self.project_root / ".GPTBridge_RuntimeSandbox"]
        if normalized_scope == "runtime":
            return normalized_scope, [self.project_root / "runtime"]
        if normalized_scope == "global":
            return normalized_scope, [self.project_root]
        return normalized_scope, None

    @staticmethod
    def _append_skip(skipped: list[dict[str, Any]], item: dict[str, Any]) -> None:
        if len(skipped) < MAX_REPORTED_SKIPS:
            skipped.append(item)

    def _relative_path(self, path: Path) -> str:
        return self._safe_resolve(path).relative_to(self.project_root).as_posix()

    def _git_snapshot(self) -> tuple[set[str], dict[str, Any]]:
        if self._git_tracked_cache is not None and self._git_status_cache is not None:
            return self._git_tracked_cache, self._git_status_cache
        if not (self.project_root / ".git").exists():
            self._git_tracked_cache = set()
            self._git_status_cache = {"available": False, "repository": False, "error": ""}
            return self._git_tracked_cache, self._git_status_cache
        try:
            completed = subprocess.run(
                ["git", "-C", str(self.project_root), "ls-files", "-z"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            if completed.returncode != 0:
                raise OSError(completed.stderr.decode("utf-8", errors="replace").strip())
            tracked = {
                item.replace("\\", "/")
                for item in completed.stdout.decode("utf-8", errors="surrogateescape").split("\0")
                if item
            }
            self._git_tracked_cache = tracked
            self._git_status_cache = {
                "available": True,
                "repository": True,
                "tracked_count": len(tracked),
                "error": "",
            }
        except (OSError, subprocess.SubprocessError) as exc:
            self._git_tracked_cache = set()
            self._git_status_cache = {
                "available": False,
                "repository": True,
                "tracked_count": 0,
                "error": str(exc),
            }
        return self._git_tracked_cache, self._git_status_cache

    def _git_protection_reason(self, path: Path, item_type: str) -> str:
        tracked, status = self._git_snapshot()
        if status.get("repository") and not status.get("available"):
            return "git protection unavailable"
        rel_path = self._relative_path(path)
        if item_type == "file" and rel_path in tracked:
            return "git-tracked file"
        prefix = rel_path.rstrip("/") + "/"
        if item_type == "directory" and any(item.startswith(prefix) for item in tracked):
            return "directory contains git-tracked files"
        return ""

    def _directory_contains_user_content(self, path: Path) -> bool:
        try:
            for current, dirnames, filenames in os.walk(path, followlinks=False):
                current_path = Path(current)
                dirnames[:] = [
                    name
                    for name in dirnames
                    if not self._is_link_or_reparse_point(current_path / name)
                ]
                for filename in filenames:
                    child = current_path / filename
                    if self._is_link_or_reparse_point(child):
                        return True
                    if child.suffix.casefold() in SOURCE_LIKE_SUFFIXES:
                        return True
        except OSError:
            return True
        return False

    def _safe_candidate(
        self,
        path: Path,
        item_type: str,
        *,
        check_git: bool = True,
    ) -> tuple[bool, str]:
        if not self._inside_project(path):
            return False, "outside project root"
        if self._is_protected(path):
            return False, "protected path"
        if self._is_link_or_reparse_point(path):
            return False, "link or reparse point"
        if not path.exists():
            return False, "path no longer exists"
        if item_type == "directory" and not path.is_dir():
            return False, "candidate is not a directory"
        if item_type == "file" and not path.is_file():
            return False, "candidate is not a file"
        if check_git:
            reason = self._git_protection_reason(path, item_type)
            if reason:
                return False, reason
        return True, ""

    def _directory_rule(
        self, name: str, *, scope: str = "global"
    ) -> dict[str, Any] | None:
        lowered = name.casefold()
        for rule in self.rules.get("directory_rules", []):
            if not isinstance(rule, dict):
                continue
            if bool(rule.get("sandbox_only")) and scope != "sandbox":
                continue
            names = {str(item).casefold() for item in rule.get("names", [])}
            patterns = [str(item).casefold() for item in rule.get("patterns", [])]
            if lowered in names or any(
                fnmatch.fnmatch(lowered, pattern) for pattern in patterns
            ):
                return rule
        return None

    def _directory_reason(self, name: str) -> tuple[str, str] | None:
        rule = self._directory_rule(name)
        if rule is None:
            return None
        return str(rule.get("reason") or "generated directory"), str(rule.get("risk") or "low")

    def _file_rule(self, path: Path, now: float) -> dict[str, Any] | None:
        name = path.name
        for rule in self.rules.get("file_rules", []):
            if not isinstance(rule, dict):
                continue
            patterns = [str(item) for item in rule.get("patterns", [])]
            if not any(fnmatch.fnmatch(name.casefold(), pattern.casefold()) for pattern in patterns):
                continue
            min_age_days = max(0.0, float(rule.get("min_age_days") or 0))
            if self._age_days(path, now) < min_age_days:
                continue
            return rule
        return None

    def _file_reason(self, path: Path, now: float) -> tuple[str, str, float] | None:
        rule = self._file_rule(path, now)
        if rule is None:
            return None
        return (
            str(rule.get("reason") or "generated file"),
            str(rule.get("risk") or "low"),
            max(0.0, float(rule.get("min_age_days") or 0)),
        )

    @staticmethod
    def _stat_identity(path: Path) -> dict[str, int]:
        info = path.stat(follow_symlinks=False)
        return {
            "device": int(getattr(info, "st_dev", 0)),
            "file_id": int(getattr(info, "st_ino", 0)),
            "mode": int(info.st_mode),
            "mtime_ns": int(info.st_mtime_ns),
            "size": int(info.st_size),
        }

    def _candidate_snapshot(self, path: Path, item_type: str) -> dict[str, Any]:
        digest = hashlib.sha256()
        root_identity = self._stat_identity(path)
        digest.update(json.dumps(root_identity, sort_keys=True).encode("utf-8"))
        total_bytes = root_identity["size"] if item_type == "file" else 0
        entry_count = 1
        if item_type == "directory":
            for current, dirnames, filenames in os.walk(path, followlinks=False):
                current_path = Path(current)
                kept_dirs: list[str] = []
                for dirname in sorted(dirnames):
                    child = current_path / dirname
                    if self._is_link_or_reparse_point(child):
                        continue
                    kept_dirs.append(dirname)
                    identity = self._stat_identity(child)
                    rel = child.relative_to(path).as_posix()
                    digest.update(f"D:{rel}:".encode("utf-8"))
                    digest.update(json.dumps(identity, sort_keys=True).encode("utf-8"))
                    entry_count += 1
                dirnames[:] = kept_dirs
                for filename in sorted(filenames):
                    child = current_path / filename
                    if self._is_link_or_reparse_point(child):
                        continue
                    identity = self._stat_identity(child)
                    rel = child.relative_to(path).as_posix()
                    digest.update(f"F:{rel}:".encode("utf-8"))
                    digest.update(json.dumps(identity, sort_keys=True).encode("utf-8"))
                    total_bytes += identity["size"]
                    entry_count += 1
        return {
            "digest": digest.hexdigest(),
            "size_bytes": total_bytes,
            "entry_count": entry_count,
            "root": root_identity,
        }

    @staticmethod
    def _summarize_items(items: list[dict[str, Any]]) -> dict[str, Any]:
        by_risk: dict[str, dict[str, Any]] = {}
        by_reason: dict[str, dict[str, Any]] = {}
        by_type: dict[str, dict[str, Any]] = {}
        for item in items:
            size = int(item.get("size_bytes") or 0)
            for bucket, key in (
                (by_risk, str(item.get("risk") or "unknown")),
                (by_reason, str(item.get("reason") or "unknown")),
                (by_type, str(item.get("type") or "unknown")),
            ):
                current = bucket.setdefault(key, {"count": 0, "size_bytes": 0})
                current["count"] += 1
                current["size_bytes"] += size
        return {
            "by_risk": by_risk,
            "by_reason": by_reason,
            "by_type": by_type,
            "largest_items": sorted(
                items,
                key=lambda item: int(item.get("size_bytes") or 0),
                reverse=True,
            )[:10],
        }

    @staticmethod
    def _risk_count(items: list[dict[str, Any]], risk: str) -> int:
        return sum(1 for item in items if str(item.get("risk") or "") == risk)

    def _cleanup_health(
        self,
        scope: str,
        items: list[dict[str, Any]],
        skipped: list[dict[str, Any]],
        errors: list[dict[str, Any]],
    ) -> dict[str, Any]:
        high_count = self._risk_count(items, "high")
        medium_count = self._risk_count(items, "medium")
        low_count = self._risk_count(items, "low")
        total_bytes = sum(int(item.get("size_bytes") or 0) for item in items)
        error_count = len(errors)
        skipped_count = len(skipped)
        safety_score = max(
            0,
            100
            - min(60, high_count * 25)
            - min(35, medium_count * 8)
            - min(30, error_count * 12)
            - min(10, skipped_count // 25),
        )
        size_mib = total_bytes / (1024 * 1024)
        calculated_penalty = int(math.log2(size_mib + 1) * 12) + len(items) // 3
        cleanliness_penalty = min(80, max(1 if items else 0, calculated_penalty))
        cleanliness_score = max(0, 100 - cleanliness_penalty)
        confidence_score = max(0, 100 - error_count * 20 - min(40, skipped_count // 5))
        if not items and not errors:
            state = "clean"
            recommendation = "目前沒有可清理項目。"
            recommended_action = "none"
        elif errors:
            state = "attention"
            recommendation = "清理前先處理掃描錯誤。"
            recommended_action = "review"
        elif high_count or medium_count:
            state = "review"
            recommendation = "包含需確認項目，只允許隔離清理。"
            recommended_action = "quarantine"
        else:
            state = "ready"
            recommendation = "低風險項目可套用已驗證計畫，仍建議先隔離。"
            recommended_action = "quarantine"
        return {
            "state": state,
            "safety_level": "high" if high_count else "medium" if medium_count else "low",
            "score": safety_score,
            "safety_score": safety_score,
            "cleanliness_score": cleanliness_score,
            "confidence_score": confidence_score,
            "item_count": len(items),
            "low_count": low_count,
            "medium_count": medium_count,
            "high_count": high_count,
            "error_count": error_count,
            "skipped_count": skipped_count,
            "total_bytes": total_bytes,
            "recommended_action": recommended_action,
            "recommendation": recommendation,
            "requires_review": bool(error_count or high_count or medium_count),
            "direct_delete_allowed": bool(items and not error_count and not high_count and not medium_count),
            "quarantine_ttl_hours": self._quarantine_ttl_hours(),
            "scope": scope,
        }

    def _quarantine_ttl_hours(self) -> int:
        return max(1, int(self.rules.get("quarantine_ttl_hours") or DEFAULT_QUARANTINE_TTL_HOURS))

    def _plan_ttl_minutes(self) -> int:
        return max(1, int(self.rules.get("plan_ttl_minutes") or DEFAULT_PLAN_TTL_MINUTES))

    def update_preferences(
        self,
        *,
        automation_enabled: bool | None = None,
        quarantine_ttl_hours: int | None = None,
    ) -> dict[str, Any]:
        with self._mutation_guard("preferences") as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": True,
                    "message": str(lock.get("message") or "project cleaner is busy"),
                }
            return self._update_preferences_unlocked(
                automation_enabled=automation_enabled,
                quarantine_ttl_hours=quarantine_ttl_hours,
            )

    def _update_preferences_unlocked(
        self,
        *,
        automation_enabled: bool | None = None,
        quarantine_ttl_hours: int | None = None,
    ) -> dict[str, Any]:
        override: dict[str, Any] = {}
        if self.rules_override_path.exists():
            try:
                loaded = json.loads(self.rules_override_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    override = loaded
            except (OSError, json.JSONDecodeError) as exc:
                return {"ok": False, "message": f"rule override is invalid: {exc}"}
        if automation_enabled is not None:
            automation = override.get("automation")
            if not isinstance(automation, dict):
                automation = {}
            automation["enabled"] = bool(automation_enabled)
            override["automation"] = automation
        if quarantine_ttl_hours is not None:
            override["quarantine_ttl_hours"] = max(1, min(24 * 365, int(quarantine_ttl_hours)))
        override.setdefault("schema_version", PLAN_SCHEMA_VERSION)
        self._atomic_write_json(self.rules_override_path, override)
        self.rules, self.rule_warnings = self._load_rules()
        self._append_history(
            "preferences",
            ok=True,
            automation_enabled=bool(self.rules.get("automation", {}).get("enabled")),
            quarantine_ttl_hours=self._quarantine_ttl_hours(),
        )
        return {
            "ok": True,
            "override_path": str(self.rules_override_path),
            "automation": self.rules.get("automation", {}),
            "quarantine_ttl_hours": self._quarantine_ttl_hours(),
            "message": "project cleaner preferences updated",
        }

    def _plan_key(self) -> bytes:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self._harden_private_path(self.runtime_root)
        if self.key_path.exists():
            return self.key_path.read_bytes()
        key = secrets.token_bytes(32)
        try:
            with self.key_path.open("xb") as target:
                target.write(key)
            try:
                self.key_path.chmod(0o600)
            except OSError:
                pass
            return key
        except FileExistsError:
            return self.key_path.read_bytes()

    def _plan_signature(self, payload: dict[str, Any]) -> str:
        signable = {key: value for key, value in payload.items() if key != "plan_token"}
        encoded = json.dumps(
            signable,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hmac.new(self._plan_key(), encoded, hashlib.sha256).hexdigest()

    def _persist_plan(self, plan: dict[str, Any]) -> tuple[str, str]:
        plan_id = uuid.uuid4().hex
        created_at = datetime.now(timezone.utc)
        document = {
            "schema_version": PLAN_SCHEMA_VERSION,
            "plan_id": plan_id,
            "created_at": created_at.isoformat(),
            "expires_at": (created_at + timedelta(minutes=self._plan_ttl_minutes())).isoformat(),
            **plan,
        }
        token = self._plan_signature(document)
        document["plan_token"] = token
        self._atomic_write_json(self.plan_root / f"{plan_id}.json", document)
        return plan_id, token

    def _load_plan(self, plan_id: str, plan_token: str) -> tuple[dict[str, Any] | None, str]:
        clean_id = str(plan_id or "").strip().lower()
        if len(clean_id) != 32 or any(char not in "0123456789abcdef" for char in clean_id):
            return None, "valid preview plan is required"
        path = self.plan_root / f"{clean_id}.json"
        if not path.exists():
            return None, "preview plan not found"
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"preview plan is invalid: {exc}"
        if not isinstance(document, dict):
            return None, "preview plan is invalid"
        expected = self._plan_signature(document)
        stored = str(document.get("plan_token") or "")
        supplied = str(plan_token or "")
        if not stored or not supplied or not hmac.compare_digest(expected, stored) or not hmac.compare_digest(stored, supplied):
            return None, "preview plan signature mismatch"
        expires_at = _parse_iso(str(document.get("expires_at") or ""))
        if expires_at is None or expires_at <= datetime.now(timezone.utc):
            return None, "preview plan expired; run preview again"
        return document, ""

    def plan_cleanup(self, scope: str, *, now: float | None = None) -> dict[str, Any]:
        requested_scope = str(scope or "global").strip().lower()
        normalized_scope, candidate_roots = self._candidate_roots(requested_scope)
        if candidate_roots is None:
            return {
                "ok": False,
                "scope": requested_scope,
                "requested_scope": requested_scope,
                "items": [],
                "skipped": [],
                "errors": [],
                "message": f"unsupported cleanup scope: {requested_scope}",
            }
        self._emit_progress("scan", 2, "建立清理計畫", scope=normalized_scope)
        now = time.time() if now is None else now
        items: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        excluded_names = {
            str(item).casefold() for item in self.rules.get("excluded_directory_names", [])
        }
        _tracked, git_status = self._git_snapshot()
        if git_status.get("repository") and not git_status.get("available"):
            errors.append({"path": ".git", "type": "git", "message": git_status.get("error") or "git unavailable"})

        for root_index, raw_root in enumerate(candidate_roots):
            root = self._safe_resolve(raw_root)
            if not root.exists():
                continue
            if not self._inside_project(root) or self._is_protected(root):
                self._append_skip(skipped, {"path": str(raw_root), "reason": "protected or outside project"})
                continue
            for current_raw, dirnames, filenames in os.walk(root, followlinks=False):
                current = Path(current_raw)
                if self._is_protected(current):
                    dirnames[:] = []
                    continue
                kept: list[str] = []
                for dirname in sorted(dirnames):
                    child = current / dirname
                    if dirname.casefold() in excluded_names or self._is_protected(child):
                        continue
                    if self._is_link_or_reparse_point(child):
                        self._append_skip(skipped, {"path": self._relative_path(child), "type": "directory", "reason": "link or reparse point"})
                        continue
                    rule = self._directory_rule(dirname, scope=normalized_scope)
                    if rule is None:
                        kept.append(dirname)
                        continue
                    min_age_days = max(0.0, float(rule.get("min_age_days") or 0))
                    if self._age_days(child, now) < min_age_days:
                        continue
                    safe, reason = self._safe_candidate(child, "directory")
                    if safe and bool(rule.get("protect_user_content")) and self._directory_contains_user_content(child):
                        safe, reason = False, "directory contains source-like user content"
                    if not safe:
                        self._append_skip(skipped, {"path": self._relative_path(child), "type": "directory", "reason": reason})
                        continue
                    try:
                        snapshot = self._candidate_snapshot(child, "directory")
                        rel_path = self._relative_path(child)
                        item_id = hashlib.sha256(f"directory:{rel_path}:{snapshot['digest']}".encode("utf-8")).hexdigest()[:24]
                        items.append(
                            {
                                "item_id": item_id,
                                "path": rel_path,
                                "type": "directory",
                                "size_bytes": snapshot["size_bytes"],
                                "entry_count": snapshot["entry_count"],
                                "reason": str(rule.get("reason") or "generated directory"),
                                "rule_id": str(rule.get("id") or "directory-rule"),
                                "risk": str(rule.get("risk") or "low"),
                                "age_days": round(self._age_days(child, now), 2),
                                "min_age_days": min_age_days,
                                "fingerprint": snapshot,
                            }
                        )
                    except OSError as exc:
                        errors.append({"path": self._relative_path(child), "type": "directory", "message": str(exc)})
                    continue
                dirnames[:] = kept

                for filename in sorted(filenames):
                    path = current / filename
                    if self._is_protected(path) or self._is_link_or_reparse_point(path):
                        continue
                    rule = self._file_rule(path, now)
                    if rule is None:
                        continue
                    safe, reason = self._safe_candidate(path, "file")
                    if not safe:
                        self._append_skip(skipped, {"path": self._relative_path(path), "type": "file", "reason": reason})
                        continue
                    try:
                        snapshot = self._candidate_snapshot(path, "file")
                        rel_path = self._relative_path(path)
                        item_id = hashlib.sha256(f"file:{rel_path}:{snapshot['digest']}".encode("utf-8")).hexdigest()[:24]
                        items.append(
                            {
                                "item_id": item_id,
                                "path": rel_path,
                                "type": "file",
                                "size_bytes": snapshot["size_bytes"],
                                "entry_count": 1,
                                "reason": str(rule.get("reason") or "generated file"),
                                "rule_id": str(rule.get("id") or "file-rule"),
                                "risk": str(rule.get("risk") or "low"),
                                "age_days": round(self._age_days(path, now), 2),
                                "min_age_days": max(0.0, float(rule.get("min_age_days") or 0)),
                                "fingerprint": snapshot,
                            }
                        )
                    except OSError as exc:
                        errors.append({"path": self._relative_path(path), "type": "file", "message": str(exc)})
            self._emit_progress(
                "scan",
                15 + int((root_index + 1) / max(1, len(candidate_roots)) * 65),
                "掃描清理候選",
                item_count=len(items),
            )

        total_bytes = sum(int(item.get("size_bytes") or 0) for item in items)
        file_count = sum(1 for item in items if item.get("type") == "file")
        dir_count = sum(1 for item in items if item.get("type") == "directory")
        health = self._cleanup_health(normalized_scope, items, skipped, errors)
        plan_payload = {
            "scope": normalized_scope,
            "requested_scope": requested_scope,
            "items": items,
            "item_count": len(items),
            "file_count": file_count,
            "dir_count": dir_count,
            "total_bytes": total_bytes,
            "summary": self._summarize_items(items),
            "health": health,
            "skipped": skipped,
            "skipped_count": len(skipped),
            "errors": errors,
            "error_count": len(errors),
            "git": git_status,
            "rule_schema_version": self.rules.get("schema_version"),
        }
        plan_id, plan_token = self._persist_plan(plan_payload)
        self._append_history(
            "preview",
            ok=True,
            scope=normalized_scope,
            plan_id=plan_id,
            item_count=len(items),
            bytes=total_bytes,
        )
        self._emit_progress("scan", 100, "清理計畫完成", plan_id=plan_id, item_count=len(items))
        return {
            "ok": True,
            **plan_payload,
            "plan_id": plan_id,
            "plan_token": plan_token,
            "plan_expires_in_minutes": self._plan_ttl_minutes(),
            "message": f"{normalized_scope} cleanup plan completed (items={len(items)})",
        }

    @contextmanager
    def _open_shared_read(self, source: Path) -> Iterator[BinaryIO]:
        if os.name != "nt":
            with source.open("rb") as source_file:
                yield source_file
            return
        import msvcrt
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        handle = create_file(
            str(source),
            0x80000000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x00000080 | 0x08000000,
            None,
        )
        invalid = wintypes.HANDLE(-1).value
        if handle in (None, invalid):
            error = ctypes.get_last_error()
            raise OSError(error, ctypes.FormatError(error), str(source))
        try:
            descriptor = msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
        except OSError:
            close_handle(handle)
            raise
        with os.fdopen(descriptor, "rb") as source_file:
            yield source_file

    def _file_sha256(self, path: Path) -> str:
        chunk_size = max(64 * 1024, int(self.rules.get("analysis", {}).get("hash_chunk_bytes") or 1024 * 1024))
        digest = hashlib.sha256()
        with self._open_shared_read(path) as source:
            while chunk := source.read(chunk_size):
                digest.update(chunk)
        return digest.hexdigest()

    def _content_digest(self, path: Path, item_type: str) -> str:
        if item_type == "file":
            return self._file_sha256(path)
        digest = hashlib.sha256()
        for current, dirnames, filenames in os.walk(path, followlinks=False):
            current_path = Path(current)
            dirnames[:] = [
                name
                for name in sorted(dirnames)
                if not self._is_link_or_reparse_point(current_path / name)
            ]
            for filename in sorted(filenames):
                child = current_path / filename
                if self._is_link_or_reparse_point(child):
                    continue
                rel = child.relative_to(path).as_posix()
                digest.update(rel.encode("utf-8"))
                digest.update(self._file_sha256(child).encode("ascii"))
        return digest.hexdigest()

    def _is_locked(self, path: Path) -> bool:
        if os.name != "nt":
            return False
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        handle = create_file(
            str(path),
            0x00010000,
            0x00000001 | 0x00000002 | 0x00000004,
            None,
            3,
            0x02000000 if path.is_dir() else 0x00000080,
            None,
        )
        invalid = wintypes.HANDLE(-1).value
        if handle in (None, invalid):
            return ctypes.get_last_error() in {32, 33}
        kernel32.CloseHandle(handle)
        return False

    @staticmethod
    def _locking_processes(path: Path) -> list[dict[str, Any]]:
        if os.name != "nt":
            return []
        try:
            from ctypes import wintypes

            class RM_UNIQUE_PROCESS(ctypes.Structure):
                _fields_ = [("dwProcessId", wintypes.DWORD), ("ProcessStartTime", wintypes.FILETIME)]

            class RM_PROCESS_INFO(ctypes.Structure):
                _fields_ = [
                    ("Process", RM_UNIQUE_PROCESS),
                    ("strAppName", wintypes.WCHAR * 256),
                    ("strServiceShortName", wintypes.WCHAR * 64),
                    ("ApplicationType", wintypes.UINT),
                    ("AppStatus", wintypes.ULONG),
                    ("TSSessionId", wintypes.DWORD),
                    ("bRestartable", wintypes.BOOL),
                ]

            restart_manager = ctypes.WinDLL("Rstrtmgr")
            session = wintypes.DWORD()
            key = ctypes.create_unicode_buffer(33)
            if restart_manager.RmStartSession(ctypes.byref(session), 0, key) != 0:
                return []
            try:
                resources = (wintypes.LPCWSTR * 1)(str(path))
                if restart_manager.RmRegisterResources(session, 1, resources, 0, None, 0, None) != 0:
                    return []
                needed = wintypes.UINT(0)
                count = wintypes.UINT(0)
                reason = wintypes.DWORD(0)
                result = restart_manager.RmGetList(
                    session,
                    ctypes.byref(needed),
                    ctypes.byref(count),
                    None,
                    ctypes.byref(reason),
                )
                if result != 234 or needed.value == 0:
                    return []
                entries = (RM_PROCESS_INFO * needed.value)()
                count = wintypes.UINT(needed.value)
                if restart_manager.RmGetList(
                    session,
                    ctypes.byref(needed),
                    ctypes.byref(count),
                    entries,
                    ctypes.byref(reason),
                ) != 0:
                    return []
                return [
                    {
                        "pid": int(entries[index].Process.dwProcessId),
                        "name": str(entries[index].strAppName),
                        "restartable": bool(entries[index].bRestartable),
                    }
                    for index in range(count.value)
                ]
            finally:
                restart_manager.RmEndSession(session)
        except Exception:
            return []

    def _unique_quarantine_target(self, batch_dir: Path, rel_path: str) -> Path:
        target = batch_dir / "items" / rel_path
        if not target.exists():
            return target
        for index in range(1, 1000):
            candidate = target.with_name(f"{target.stem}.{index}{target.suffix}")
            if not candidate.exists():
                return candidate
        return target.with_name(f"{target.stem}.{uuid.uuid4().hex[:8]}{target.suffix}")

    @staticmethod
    def _batch_document_sort_key(
        path: Path,
        payload: dict[str, Any],
    ) -> tuple[int, float, int]:
        try:
            revision = max(0, int(payload.get("revision") or 0))
        except (TypeError, ValueError):
            revision = 0
        updated_at = _parse_iso(str(payload.get("updated_at") or ""))
        updated_timestamp = updated_at.timestamp() if updated_at is not None else 0.0
        # The journal is the write-ahead record, so prefer it only when both
        # monotonic revision and timestamp are otherwise identical.
        journal_tiebreaker = int(path.name == "journal.json")
        return revision, updated_timestamp, journal_tiebreaker

    def _valid_batch_documents(
        self,
        batch_dir: Path,
    ) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
        valid: list[tuple[Path, dict[str, Any]]] = []
        errors: list[str] = []
        found = False
        for path in (batch_dir / "manifest.json", batch_dir / "journal.json"):
            if not path.exists():
                continue
            found = True
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                errors.append(f"{path.name}: {exc}")
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
                errors.append(f"{path.name}: invalid items")
                continue
            valid.append((path, payload))
        if not found:
            errors.append("quarantine manifest not found")
        return valid, errors

    def _batch_document_path(self, batch_dir: Path) -> Path | None:
        valid, _errors = self._valid_batch_documents(batch_dir)
        if not valid:
            return None
        return max(
            valid,
            key=lambda item: self._batch_document_sort_key(item[0], item[1]),
        )[0]

    def _read_batch_document(self, batch_dir: Path) -> tuple[dict[str, Any] | None, str]:
        valid, errors = self._valid_batch_documents(batch_dir)
        if not valid:
            if errors == ["quarantine manifest not found"]:
                return None, errors[0]
            return None, f"invalid quarantine documents: {'; '.join(errors)}"
        _path, payload = max(
            valid,
            key=lambda item: self._batch_document_sort_key(item[0], item[1]),
        )
        return payload, ""

    def _write_batch_document(self, batch_dir: Path, document: dict[str, Any]) -> None:
        persisted, _error = self._read_batch_document(batch_dir)
        persisted_revision = 0
        if persisted is not None:
            try:
                persisted_revision = max(0, int(persisted.get("revision") or 0))
            except (TypeError, ValueError):
                persisted_revision = 0
        try:
            document_revision = max(0, int(document.get("revision") or 0))
        except (TypeError, ValueError):
            document_revision = 0
        document["revision"] = max(persisted_revision, document_revision) + 1
        document["updated_at"] = self._iso_now()
        self._atomic_write_json(batch_dir / "journal.json", document)
        if str(document.get("status") or "legacy") != "applying":
            self._atomic_write_json(batch_dir / "manifest.json", document)

    @staticmethod
    def _is_restorable_item(item: dict[str, Any]) -> bool:
        status = str(item.get("status") or "")
        return bool(item.get("quarantine_path")) and (
            status in {"moved", "moving", "pending", "error"}
            or (not status and bool(item.get("original_path")))
        )

    def cleanup_garbage(
        self,
        scope: str,
        dry_run: bool = False,
        *,
        quarantine: bool = False,
        quarantine_ttl_hours: int | None = None,
        plan_id: str = "",
        plan_token: str = "",
        selected_item_ids: list[str] | None = None,
        confirm_direct_delete: bool = False,
    ) -> dict[str, Any]:
        if dry_run:
            return self._cleanup_garbage_unlocked(
                scope,
                dry_run=True,
                quarantine=quarantine,
                quarantine_ttl_hours=quarantine_ttl_hours,
                plan_id=plan_id,
                plan_token=plan_token,
                selected_item_ids=selected_item_ids,
                confirm_direct_delete=confirm_direct_delete,
            )
        with self._mutation_guard("apply") as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": True,
                    "scope": str(scope or ""),
                    "dry_run": False,
                    "quarantine": quarantine,
                    "cleaned_files": 0,
                    "cleaned_dirs": 0,
                    "cleaned_bytes": 0,
                    "permanently_deleted": 0,
                    "message": str(lock.get("message") or "project cleaner is busy"),
                }
            return self._cleanup_garbage_unlocked(
                scope,
                dry_run=False,
                quarantine=quarantine,
                quarantine_ttl_hours=quarantine_ttl_hours,
                plan_id=plan_id,
                plan_token=plan_token,
                selected_item_ids=selected_item_ids,
                confirm_direct_delete=confirm_direct_delete,
            )

    def _cleanup_garbage_unlocked(
        self,
        scope: str,
        dry_run: bool = False,
        *,
        quarantine: bool = False,
        quarantine_ttl_hours: int | None = None,
        plan_id: str = "",
        plan_token: str = "",
        selected_item_ids: list[str] | None = None,
        confirm_direct_delete: bool = False,
    ) -> dict[str, Any]:
        if dry_run:
            plan = self.plan_cleanup(scope)
            return {
                **plan,
                "dry_run": True,
                "quarantine": quarantine,
                "cleaned_files": 0,
                "cleaned_dirs": 0,
                "cleaned_bytes": 0,
                "planned_files": int(plan.get("file_count") or 0),
                "planned_dirs": int(plan.get("dir_count") or 0),
                "planned_bytes": int(plan.get("total_bytes") or 0),
            }

        direct_delete_requested = not quarantine
        # All cleanup mutations are recoverable. The legacy direct-delete
        # controls remain accepted for API compatibility but can no longer
        # bypass quarantine.
        quarantine = True
        plan, plan_error = self._load_plan(plan_id, plan_token)
        if plan is None:
            return {
                "ok": False,
                "scope": str(scope or ""),
                "dry_run": False,
                "quarantine": quarantine,
                "cleaned_files": 0,
                "cleaned_dirs": 0,
                "cleaned_bytes": 0,
                "message": plan_error,
            }
        if str(plan.get("scope")) != str(scope or "").strip().lower().replace("project", "global"):
            return {"ok": False, "message": "preview plan scope mismatch", "scope": scope}

        all_items = [item for item in plan.get("items", []) if isinstance(item, dict)]
        selected = {str(item) for item in (selected_item_ids or []) if str(item).strip()}
        items = [item for item in all_items if not selected or str(item.get("item_id")) in selected]
        if selected and len(items) != len(selected):
            return {"ok": False, "message": "selected cleanup items do not match preview plan", "scope": scope}
        ttl_hours = max(1, int(quarantine_ttl_hours or self._quarantine_ttl_hours()))
        created_at = datetime.now(timezone.utc)
        batch_name = f"{created_at.strftime('%Y%m%d_%H%M%S_%f')}-{uuid.uuid4().hex[:8]}"
        batch_dir = self.quarantine_root / batch_name
        document: dict[str, Any] | None = None
        if quarantine:
            batch_dir.mkdir(parents=True, exist_ok=False)
            self._harden_private_path(batch_dir)
            journal_items: list[dict[str, Any]] = []
            reserved_paths: set[str] = set()
            for item in items:
                rel_path = str(item.get("path") or "")
                candidate = (Path("items") / rel_path).as_posix()
                if (
                    not rel_path
                    or Path(rel_path).is_absolute()
                    or ".." in Path(rel_path).parts
                    or candidate.casefold() in reserved_paths
                ):
                    item_id = str(item.get("item_id") or uuid.uuid4().hex)
                    candidate = (Path("items") / item_id / Path(rel_path).name).as_posix()
                reserved_paths.add(candidate.casefold())
                journal_items.append(
                    {
                        **item,
                        "status": "pending",
                        "quarantine_path": candidate,
                        "content_sha256": "",
                    }
                )
            document = {
                "schema_version": QUARANTINE_SCHEMA_VERSION,
                "batch_id": batch_name,
                "status": "applying",
                "created_at": created_at.isoformat(),
                "expires_at": (created_at + timedelta(hours=ttl_hours)).isoformat(),
                "pinned": False,
                "project_root": str(self.project_root),
                "scope": plan.get("scope"),
                "plan_id": plan_id,
                "items": journal_items,
                "errors": [],
                "skipped": [],
            }
            self._write_batch_document(batch_dir, document)

        cleaned_files = 0
        cleaned_dirs = 0
        cleaned_bytes = 0
        apply_errors: list[dict[str, Any]] = []
        apply_skips: list[dict[str, Any]] = []
        moved_entries = document["items"] if document is not None else []
        for index, item in enumerate(items):
            rel_path = str(item.get("path") or "")
            item_type = str(item.get("type") or "")
            target = self.project_root / rel_path
            self._emit_progress(
                "apply",
                5 + int(index / max(1, len(items)) * 90),
                "套用清理計畫",
                current_path=rel_path,
                completed=index,
                total=len(items),
            )
            safe, reason = self._safe_candidate(target, item_type)
            if not safe:
                skip = {"path": rel_path, "type": item_type, "reason": reason}
                apply_skips.append(skip)
                if document is not None:
                    moved_entries[index]["status"] = "skipped"
                    moved_entries[index]["status_reason"] = reason
                    document["skipped"] = apply_skips
                    self._write_batch_document(batch_dir, document)
                continue
            try:
                current_snapshot = self._candidate_snapshot(target, item_type)
            except OSError as exc:
                apply_errors.append({"path": rel_path, "type": item_type, "message": str(exc)})
                continue
            expected_fingerprint = item.get("fingerprint") if isinstance(item.get("fingerprint"), dict) else {}
            if current_snapshot.get("digest") != expected_fingerprint.get("digest"):
                reason = "candidate changed after preview"
                apply_skips.append({"path": rel_path, "type": item_type, "reason": reason})
                if document is not None:
                    moved_entries[index]["status"] = "skipped"
                    moved_entries[index]["status_reason"] = reason
                    document["skipped"] = apply_skips
                    self._write_batch_document(batch_dir, document)
                continue
            if self._is_locked(target):
                processes = self._locking_processes(target)
                reason = "file is in use; cleaner never forces unlock"
                apply_skips.append({"path": rel_path, "type": item_type, "reason": reason, "locked_by": processes})
                if document is not None:
                    moved_entries[index]["status"] = "locked"
                    moved_entries[index]["locked_by"] = processes
                    document["skipped"] = apply_skips
                    self._write_batch_document(batch_dir, document)
                continue
            try:
                size = int(item.get("size_bytes") or 0)
                if quarantine:
                    quarantine_rel = str(moved_entries[index]["quarantine_path"])
                    quarantine_target = (batch_dir / quarantine_rel).resolve()
                    quarantine_target.relative_to(batch_dir.resolve())
                    moved_entries[index]["status"] = "moving"
                    self._write_batch_document(batch_dir, document)
                    quarantine_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(target), str(quarantine_target))
                    moved_entries[index]["content_sha256"] = self._content_digest(quarantine_target, item_type)
                    moved_entries[index]["status"] = "moved"
                    self._write_batch_document(batch_dir, document)
                cleaned_dirs += int(item_type == "directory")
                cleaned_files += int(item_type == "file")
                cleaned_bytes += size
            except OSError as exc:
                processes = self._locking_processes(target)
                error = {"path": rel_path, "type": item_type, "message": str(exc), "locked_by": processes}
                apply_errors.append(error)
                if document is not None:
                    moved_entries[index]["status"] = "error"
                    moved_entries[index]["status_reason"] = str(exc)
                    document["errors"] = apply_errors
                    self._write_batch_document(batch_dir, document)

        manifest_path = ""
        if document is not None:
            document["status"] = "completed_with_errors" if apply_errors else "completed"
            document["errors"] = apply_errors
            document["skipped"] = apply_skips
            self._write_batch_document(batch_dir, document)
            manifest_path = str(batch_dir / "manifest.json")
            try:
                manifest_digest = self._file_sha256(Path(manifest_path))
                tombstone = {
                    "schema_version": QUARANTINE_SCHEMA_VERSION,
                    "kind": "cleanup-tombstone",
                    "status": "recoverable",
                    "created_at": self._iso_now(),
                    "batch_id": batch_name,
                    "scope": plan.get("scope"),
                    "manifest_path": manifest_path,
                    "manifest_sha256": manifest_digest,
                    "items": [
                        {
                            "original_path": str(item.get("path") or ""),
                            "recovery_path": str(item.get("quarantine_path") or ""),
                            "content_sha256": str(item.get("content_sha256") or ""),
                            "type": str(item.get("type") or ""),
                        }
                        for item in moved_entries
                        if str(item.get("status") or "") == "moved"
                    ],
                }
                self._atomic_write_json(batch_dir / "tombstone.json", tombstone)
            except OSError as exc:
                apply_errors.append(
                    {
                        "path": str(batch_dir),
                        "type": "recovery",
                        "message": f"tombstone publication failed: {exc}",
                    }
                )
        action = "quarantine"
        result = {
            "ok": not apply_errors,
            "scope": plan.get("scope"),
            "requested_scope": plan.get("requested_scope"),
            "plan_id": plan_id,
            "dry_run": False,
            "quarantine": quarantine,
            "direct_delete_requested": direct_delete_requested,
            "direct_delete_prevented": direct_delete_requested,
            "permanently_deleted": 0,
            "quarantine_path": str(batch_dir) if quarantine else "",
            "quarantine_manifest": manifest_path,
            "cleaned_files": cleaned_files,
            "cleaned_dirs": cleaned_dirs,
            "cleaned_bytes": cleaned_bytes,
            "retained_bytes": cleaned_bytes,
            "disk_space_reclaimed_bytes": 0,
            "planned_files": sum(1 for item in items if item.get("type") == "file"),
            "planned_dirs": sum(1 for item in items if item.get("type") == "directory"),
            "planned_bytes": sum(int(item.get("size_bytes") or 0) for item in items),
            "items": items,
            "summary": self._summarize_items(items),
            "health": self._cleanup_health(str(plan.get("scope")), items, apply_skips, apply_errors),
            "skipped": apply_skips,
            "skipped_count": len(apply_skips),
            "errors": apply_errors,
            "error_count": len(apply_errors),
            "message": f"{plan.get('scope')} cleanup {action} completed (items={cleaned_files + cleaned_dirs})",
        }
        self._append_history(
            action,
            ok=result["ok"],
            scope=plan.get("scope"),
            plan_id=plan_id,
            item_count=cleaned_files + cleaned_dirs,
            bytes=cleaned_bytes,
            skipped=len(apply_skips),
            errors=len(apply_errors),
            batch_id=batch_name if quarantine else "",
        )
        self._emit_progress("apply", 100, "清理計畫已完成", cleaned_bytes=cleaned_bytes)
        return result

    def _batch_expiration(self, batch: dict[str, Any], batch_dir: Path) -> datetime:
        expires = _parse_iso(str(batch.get("expires_at") or ""))
        if expires is not None:
            return expires
        try:
            modified = datetime.fromtimestamp(batch_dir.stat(follow_symlinks=False).st_mtime, timezone.utc)
        except OSError:
            modified = datetime.now(timezone.utc)
        return modified + timedelta(hours=self._quarantine_ttl_hours())

    def list_quarantine_batches(self) -> dict[str, Any]:
        batches: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        batch_roots = [
            (self.quarantine_root, False),
            (self.recovery_root / "purged", True),
        ]
        if not any(root.exists() for root, _archived in batch_roots):
            return {
                "ok": True,
                "batches": [],
                "batch_count": 0,
                "total_bytes": 0,
                "health": self._quarantine_health([]),
                "errors": [],
                "message": "quarantine is empty",
            }
        now = datetime.now(timezone.utc)
        for batch_root, archived in batch_roots:
            if not batch_root.exists():
                continue
            for child in sorted(batch_root.iterdir(), key=lambda item: item.name, reverse=True):
                if self._is_link_or_reparse_point(child) or not child.is_dir():
                    continue
                document, error = self._read_batch_document(child)
                if document is None:
                    errors.append({"path": child.name, "message": error})
                    continue
                items = [item for item in document.get("items", []) if isinstance(item, dict)]
                size_bytes = sum(int(item.get("size_bytes") or 0) for item in items if self._is_restorable_item(item))
                expiration = self._batch_expiration(document, child)
                created = _parse_iso(str(document.get("created_at") or "")) or now
                recoverable_items = [item for item in items if self._is_restorable_item(item)]
                batches.append(
                    {
                        "name": child.name,
                        "path": str(child),
                        "archived": archived,
                        "created_at": created.isoformat(),
                        "scope": str(document.get("scope") or ""),
                        "status": str(document.get("status") or "legacy"),
                        "item_count": len(items),
                        "restorable_count": len(recoverable_items),
                        "size_bytes": size_bytes,
                        "age_hours": round(max(0.0, (now - created).total_seconds() / 3600), 2),
                        "expires_at": expiration.isoformat(),
                        "expired": False if archived else now >= expiration,
                        "pinned": bool(document.get("pinned")),
                        "recoverable": str(document.get("status")) == "applying"
                        or any(str(item.get("status") or "") in {"pending", "error"} for item in recoverable_items),
                        "manifest_path": str(self._batch_document_path(child) or ""),
                    }
                )
        return {
            "ok": not errors,
            "batches": batches,
            "batch_count": len(batches),
            "total_bytes": sum(int(item.get("size_bytes") or 0) for item in batches),
            "health": self._quarantine_health(batches),
            "errors": errors,
            "message": f"quarantine batches listed (items={len(batches)})",
        }

    def _quarantine_health(self, batches: list[dict[str, Any]]) -> dict[str, Any]:
        total_bytes = sum(int(batch.get("size_bytes") or 0) for batch in batches)
        expired_count = sum(1 for batch in batches if batch.get("expired") and not batch.get("pinned"))
        incomplete_count = sum(1 for batch in batches if batch.get("recoverable"))
        score = max(0, 100 - expired_count * 10 - incomplete_count * 25)
        if not batches:
            state, recommendation = "empty", "隔離區目前沒有批次。"
        elif incomplete_count:
            state, recommendation = "attention", "有中斷交易，建議先還原或完成處理。"
        elif expired_count:
            state, recommendation = "attention", "有過期批次可清理；釘選批次不會自動移除。"
        else:
            state, recommendation = "healthy", "隔離交易完整且可還原。"
        return {
            "state": state,
            "score": score,
            "batch_count": len(batches),
            "expired_count": expired_count,
            "incomplete_count": incomplete_count,
            "pinned_count": sum(1 for batch in batches if batch.get("pinned")),
            "total_bytes": total_bytes,
            "ttl_hours": self._quarantine_ttl_hours(),
            "recommendation": recommendation,
        }

    def set_quarantine_pinned(self, batch_name: str, pinned: bool) -> dict[str, Any]:
        clean_name = Path(str(batch_name or "").strip()).name
        if not clean_name:
            return {"ok": False, "message": "quarantine batch is required"}
        with self._mutation_guard("pin" if pinned else "unpin", batch_name=clean_name) as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": True,
                    "message": str(lock.get("message") or "project cleaner is busy"),
                }
            return self._set_quarantine_pinned_unlocked(clean_name, pinned)

    def _set_quarantine_pinned_unlocked(
        self, clean_name: str, pinned: bool
    ) -> dict[str, Any]:
        batch_dir = self.quarantine_root / clean_name
        if not batch_dir.exists():
            archived = self.recovery_root / "purged" / clean_name
            if archived.exists():
                batch_dir = archived
        document, error = self._read_batch_document(batch_dir)
        if document is None:
            return {"ok": False, "message": error}
        document["pinned"] = bool(pinned)
        self._write_batch_document(batch_dir, document)
        self._append_history("pin" if pinned else "unpin", ok=True, batch_id=clean_name)
        return {"ok": True, "batch": clean_name, "pinned": bool(pinned), "message": "quarantine pin updated"}

    def purge_quarantine(self, older_than_hours: int | None = None) -> dict[str, Any]:
        with self._mutation_guard("purge") as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": True,
                    "purged_dirs": 0,
                    "purged_bytes": 0,
                    "permanently_deleted": 0,
                    "errors": [],
                    "skipped": [],
                    "message": str(lock.get("message") or "project cleaner is busy"),
                }
            return self._purge_quarantine_unlocked(older_than_hours)

    def _purge_quarantine_unlocked(
        self, older_than_hours: int | None = None
    ) -> dict[str, Any]:
        ttl = max(1, int(older_than_hours or self._quarantine_ttl_hours()))
        purged_dirs = 0
        purged_bytes = 0
        errors: list[dict[str, str]] = []
        skipped: list[dict[str, str]] = []
        archives: list[dict[str, Any]] = []
        if not self.quarantine_root.exists():
            return {
                "ok": True,
                "purged_dirs": 0,
                "purged_bytes": 0,
                "archived_dirs": 0,
                "archived_bytes": 0,
                "archives": [],
                "permanently_deleted": 0,
                "errors": [],
                "skipped": [],
                "message": "quarantine is empty",
            }
        now = datetime.now(timezone.utc)
        for child in sorted(self.quarantine_root.iterdir(), key=lambda item: item.name):
            if self._is_link_or_reparse_point(child) or not child.is_dir():
                continue
            document, error = self._read_batch_document(child)
            if document is None:
                skipped.append({"path": child.name, "reason": error})
                continue
            recoverable = any(
                isinstance(item, dict)
                and item.get("status") in {"pending", "error"}
                and item.get("quarantine_path")
                for item in document.get("items", [])
            )
            if document.get("pinned") or str(document.get("status")) == "applying" or recoverable:
                skipped.append({"path": child.name, "reason": "pinned or recoverable batch"})
                continue
            created = _parse_iso(str(document.get("created_at") or "")) or now
            configured_expiration = self._batch_expiration(document, child)
            requested_expiration = created + timedelta(hours=ttl)
            if now < min(configured_expiration, requested_expiration):
                continue
            try:
                size = sum(int(item.get("size_bytes") or 0) for item in document.get("items", []) if isinstance(item, dict))
                integrity_errors: list[str] = []
                for item in document.get("items", []):
                    if not isinstance(item, dict) or not self._is_restorable_item(item):
                        continue
                    relative = str(item.get("quarantine_path") or "").strip()
                    source = (child / relative).resolve()
                    try:
                        source.relative_to(child.resolve())
                    except ValueError:
                        integrity_errors.append(f"{relative}: escaped batch")
                        continue
                    if not source.exists() or self._is_link_or_reparse_point(source):
                        integrity_errors.append(f"{relative}: missing or unsafe")
                        continue
                    expected = str(item.get("content_sha256") or "")
                    actual = self._content_digest(
                        source,
                        str(item.get("type") or "file"),
                    )
                    if not expected or actual != expected:
                        integrity_errors.append(f"{relative}: SHA-256 mismatch")
                if integrity_errors:
                    skipped.append(
                        {
                            "path": child.name,
                            "reason": "; ".join(integrity_errors),
                        }
                    )
                    continue

                archive_root = self.recovery_root / "purged"
                archive_root.mkdir(parents=True, exist_ok=True)
                self._harden_private_path(self.recovery_root)
                self._harden_private_path(archive_root)
                archive_dir = archive_root / child.name
                if archive_dir.exists():
                    skipped.append(
                        {
                            "path": child.name,
                            "reason": "recovery archive destination already exists",
                        }
                    )
                    continue
                os.replace(child, archive_dir)
                document["status"] = "archived"
                document["archived_at"] = self._iso_now()
                document["archive_path"] = str(archive_dir)
                self._write_batch_document(archive_dir, document)
                manifest_document = (
                    archive_dir / "manifest.json"
                    if (archive_dir / "manifest.json").exists()
                    else archive_dir / "journal.json"
                )
                manifest_sha256 = self._file_sha256(manifest_document)
                purge_tombstone = {
                    "schema_version": QUARANTINE_SCHEMA_VERSION,
                    "kind": "purge-tombstone",
                    "status": "recoverable",
                    "created_at": self._iso_now(),
                    "batch_id": child.name,
                    "original_path": str(child),
                    "recovery_path": str(archive_dir),
                    "manifest_path": str(manifest_document),
                    "manifest_sha256": manifest_sha256,
                    "permanently_deleted": 0,
                }
                self._atomic_write_json(
                    archive_dir / "purge-tombstone.json",
                    purge_tombstone,
                )
                purged_dirs += 1
                purged_bytes += size
                archives.append(purge_tombstone)
            except OSError as exc:
                errors.append({"path": str(child), "message": str(exc)})
        result = {
            "ok": not errors,
            "purged_dirs": purged_dirs,
            "purged_bytes": purged_bytes,
            "archived_dirs": purged_dirs,
            "archived_bytes": purged_bytes,
            "disk_space_reclaimed_bytes": 0,
            "archives": archives,
            "permanently_deleted": 0,
            "errors": errors,
            "skipped": skipped,
            "message": f"quarantine archival completed (dirs={purged_dirs})",
        }
        self._append_history("purge", ok=result["ok"], item_count=purged_dirs, bytes=purged_bytes, errors=len(errors))
        return result

    @staticmethod
    def _unique_restore_destination(destination: Path) -> Path:
        for index in range(1, 1000):
            candidate = destination.with_name(f"{destination.stem}.restored-{index}{destination.suffix}")
            if not candidate.exists():
                return candidate
        return destination.with_name(f"{destination.stem}.restored-{uuid.uuid4().hex[:8]}{destination.suffix}")

    def restore_quarantine(
        self, batch_name: str, *, conflict_strategy: str = "skip"
    ) -> dict[str, Any]:
        clean_name = Path(str(batch_name or "").strip()).name
        if not clean_name:
            return {"ok": False, "message": "quarantine batch is required"}
        with self._mutation_guard("restore", batch_name=clean_name) as lock:
            if not lock.get("acquired"):
                return {
                    "ok": False,
                    "busy": True,
                    "restored": 0,
                    "renamed": 0,
                    "skipped": [],
                    "errors": [],
                    "message": str(lock.get("message") or "project cleaner is busy"),
                }
            return self._restore_quarantine_unlocked(
                clean_name,
                conflict_strategy=conflict_strategy,
            )

    def _restore_quarantine_unlocked(
        self, batch_name: str, *, conflict_strategy: str = "skip"
    ) -> dict[str, Any]:
        clean_name = Path(str(batch_name or "").strip()).name
        live_batch = self.quarantine_root / clean_name
        archived_batch = self.recovery_root / "purged" / clean_name
        selected_root = self.quarantine_root
        selected_batch = live_batch
        if not live_batch.exists() and archived_batch.exists():
            selected_root = self.recovery_root / "purged"
            selected_batch = archived_batch
        batch_dir = selected_batch.resolve()
        try:
            batch_dir.relative_to(selected_root.resolve())
        except ValueError:
            return {"ok": False, "message": "invalid quarantine batch"}
        document, error = self._read_batch_document(batch_dir)
        if document is None:
            return {"ok": False, "message": error}
        strategy = conflict_strategy if conflict_strategy in {"skip", "rename"} else "skip"
        restored = 0
        renamed = 0
        skipped: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        items = [item for item in document.get("items", []) if isinstance(item, dict)]
        for index, item in enumerate(items):
            if not self._is_restorable_item(item):
                continue
            original_rel = str(item.get("path") or item.get("original_path") or "").strip()
            quarantine_rel = str(item.get("quarantine_path") or "").strip()
            if not original_rel or not quarantine_rel:
                continue
            source = (batch_dir / quarantine_rel).resolve()
            destination = (self.project_root / original_rel).resolve()
            try:
                source.relative_to(batch_dir)
                destination.relative_to(self.project_root)
            except ValueError:
                skipped.append({"path": original_rel, "reason": "invalid restore path"})
                continue
            if not source.exists() or self._is_link_or_reparse_point(source):
                skipped.append({"path": original_rel, "reason": "quarantine item missing or unsafe"})
                continue
            expected_digest = str(item.get("content_sha256") or "")
            if expected_digest:
                try:
                    actual_digest = self._content_digest(source, str(item.get("type") or "file"))
                except OSError as exc:
                    errors.append({"path": original_rel, "message": str(exc)})
                    continue
                if actual_digest != expected_digest:
                    skipped.append({"path": original_rel, "reason": "quarantine integrity mismatch"})
                    continue
            if destination.exists():
                if strategy == "rename":
                    destination = self._unique_restore_destination(destination)
                    renamed += 1
                else:
                    skipped.append({"path": original_rel, "reason": "destination already exists"})
                    continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
                item["status"] = "restored"
                item["restored_path"] = destination.relative_to(self.project_root).as_posix()
                restored += 1
                document["items"] = items
                self._write_batch_document(batch_dir, document)
            except OSError as exc:
                errors.append({"path": original_rel, "message": str(exc), "locked_by": self._locking_processes(destination)})
        if not any(item.get("status") == "moved" for item in items):
            document["status"] = "restored"
        document["items"] = items
        document["restore_errors"] = errors
        document["restore_skips"] = skipped
        self._write_batch_document(batch_dir, document)
        result = {
            "ok": not errors,
            "restored": restored,
            "renamed": renamed,
            "conflict_strategy": strategy,
            "skipped": skipped,
            "errors": errors,
            "message": f"quarantine restore completed (items={restored})",
        }
        self._append_history("restore", ok=result["ok"], batch_id=clean_name, item_count=restored, renamed=renamed, errors=len(errors))
        return result

    def _iter_analysis_files(self, scope: str) -> Iterator[Path]:
        normalized, roots = self._candidate_roots(scope)
        if roots is None:
            return
        excluded = {str(item).casefold() for item in self.rules.get("excluded_directory_names", [])}
        for root in roots:
            if not root.exists() or self._is_protected(root):
                continue
            for current, dirnames, filenames in os.walk(root, followlinks=False):
                current_path = Path(current)
                kept: list[str] = []
                for dirname in sorted(dirnames):
                    child = current_path / dirname
                    if dirname.casefold() in excluded or self._is_protected(child) or self._is_link_or_reparse_point(child):
                        continue
                    kept.append(dirname)
                dirnames[:] = kept
                for filename in sorted(filenames):
                    path = current_path / filename
                    if self._is_protected(path) or self._is_link_or_reparse_point(path):
                        continue
                    try:
                        if path.is_file():
                            yield path
                    except OSError:
                        continue

    def _quick_file_hash(self, path: Path, size: int) -> str:
        digest = hashlib.sha256()
        with self._open_shared_read(path) as source:
            digest.update(source.read(64 * 1024))
            if size > 128 * 1024:
                source.seek(max(0, size - 64 * 1024))
                digest.update(source.read(64 * 1024))
        return digest.hexdigest()

    def analyze_storage(self, scope: str = "global") -> dict[str, Any]:
        normalized, roots = self._candidate_roots(str(scope or "global").strip().lower())
        if roots is None:
            return {"ok": False, "message": f"unsupported cleanup scope: {scope}"}
        analysis = self.rules.get("analysis", {}) if isinstance(self.rules.get("analysis"), dict) else {}
        duplicate_min = max(1, int(analysis.get("duplicate_min_size_bytes") or 1024 * 1024))
        large_min = max(1, int(analysis.get("large_file_min_size_bytes") or 10 * 1024 * 1024))
        configured_large_age = analysis.get("large_file_min_age_days")
        large_age = max(0.0, float(14 if configured_large_age is None else configured_large_age))
        now = time.time()
        size_groups: dict[int, list[Path]] = {}
        large_files: list[dict[str, Any]] = []
        file_count = 0
        total_bytes = 0
        self._emit_progress("analyze", 2, "分析儲存空間", scope=normalized)
        for path in self._iter_analysis_files(normalized):
            try:
                info = path.stat(follow_symlinks=False)
            except OSError:
                continue
            file_count += 1
            total_bytes += info.st_size
            if info.st_size >= duplicate_min:
                size_groups.setdefault(int(info.st_size), []).append(path)
            age_days = max(0.0, (now - info.st_mtime) / SECONDS_PER_DAY)
            if info.st_size >= large_min and age_days >= large_age:
                large_files.append(
                    {
                        "path": self._relative_path(path),
                        "size_bytes": int(info.st_size),
                        "age_days": round(age_days, 2),
                    }
                )
        duplicate_groups: list[dict[str, Any]] = []
        candidates = [(size, paths) for size, paths in size_groups.items() if len(paths) > 1]
        for group_index, (size, paths) in enumerate(candidates):
            quick_groups: dict[str, list[Path]] = {}
            for path in paths:
                try:
                    quick_groups.setdefault(self._quick_file_hash(path, size), []).append(path)
                except OSError:
                    continue
            for quick_paths in quick_groups.values():
                if len(quick_paths) < 2:
                    continue
                full_groups: dict[str, list[Path]] = {}
                for path in quick_paths:
                    try:
                        full_groups.setdefault(self._file_sha256(path), []).append(path)
                    except OSError:
                        continue
                for digest, duplicate_paths in full_groups.items():
                    if len(duplicate_paths) < 2:
                        continue
                    duplicate_groups.append(
                        {
                            "sha256": digest,
                            "size_bytes": size,
                            "copies": len(duplicate_paths),
                            "wasted_bytes": size * (len(duplicate_paths) - 1),
                            "paths": [self._relative_path(path) for path in duplicate_paths],
                        }
                    )
            self._emit_progress(
                "analyze",
                60 + int((group_index + 1) / max(1, len(candidates)) * 35),
                "比對重複檔案",
                duplicate_groups=len(duplicate_groups),
            )
        duplicate_groups.sort(key=lambda item: int(item["wasted_bytes"]), reverse=True)
        large_files.sort(key=lambda item: int(item["size_bytes"]), reverse=True)
        result = {
            "ok": True,
            "scope": normalized,
            "file_count": file_count,
            "total_bytes": total_bytes,
            "duplicate_groups": duplicate_groups,
            "duplicate_group_count": len(duplicate_groups),
            "duplicate_wasted_bytes": sum(int(item["wasted_bytes"]) for item in duplicate_groups),
            "large_stale_files": large_files[:100],
            "large_stale_count": len(large_files),
            "message": f"storage analysis completed (duplicates={len(duplicate_groups)})",
        }
        self._append_history(
            "analyze",
            ok=True,
            scope=normalized,
            item_count=file_count,
            duplicate_groups=len(duplicate_groups),
            duplicate_wasted_bytes=result["duplicate_wasted_bytes"],
        )
        self._emit_progress("analyze", 100, "儲存空間分析完成")
        return result

    @staticmethod
    def _process_is_alive(process_id: int) -> bool:
        if process_id <= 0:
            return False
        if process_id == os.getpid():
            return True
        if os.name == "nt":
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.GetExitCodeProcess.argtypes = [
                wintypes.HANDLE,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.GetExitCodeProcess.restype = wintypes.BOOL
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            process = kernel32.OpenProcess(0x1000, False, process_id)
            if not process:
                return ctypes.get_last_error() == 5
            try:
                exit_code = wintypes.DWORD()
                if not kernel32.GetExitCodeProcess(
                    process,
                    ctypes.byref(exit_code),
                ):
                    return True
                return exit_code.value == 259
            finally:
                kernel32.CloseHandle(process)
        try:
            os.kill(process_id, 0)
            return True
        except PermissionError:
            return True
        except OSError:
            return False

    @staticmethod
    def _is_sha256_text(value: str) -> bool:
        normalized = str(value or "").strip()
        return len(normalized) == 64 and all(
            character in "0123456789abcdefABCDEF"
            for character in normalized
        )

    def _repair_anomaly_candidates(
        self,
        *,
        include_shared: bool,
        now: float | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Find only deterministic anomalies that have a recoverable repair."""

        current_time = time.time() if now is None else float(now)
        candidates: list[dict[str, Any]] = []
        diagnostics: list[dict[str, Any]] = []
        seen: set[str] = set()

        def add_candidate(
            path: Path,
            *,
            layer: str,
            anomaly: str,
            reason: str,
        ) -> None:
            absolute = Path(os.path.abspath(path))
            key = os.path.normcase(str(absolute))
            if key in seen or not absolute.exists():
                return
            seen.add(key)
            if not self._inside_project(absolute):
                diagnostics.append(
                    {
                        "path": str(absolute),
                        "layer": layer,
                        "anomaly": anomaly,
                        "repairable": False,
                        "reason": "path is outside project root",
                    }
                )
                return
            if self._is_link_or_reparse_point(absolute):
                diagnostics.append(
                    {
                        "path": self._relative_path(absolute),
                        "layer": layer,
                        "anomaly": anomaly,
                        "repairable": False,
                        "reason": "link or reparse point requires manual review",
                    }
                )
                return
            item_type = "directory" if absolute.is_dir() else "file"
            if item_type == "file" and not absolute.is_file():
                return
            try:
                snapshot = self._candidate_snapshot(absolute, item_type)
            except OSError as exc:
                diagnostics.append(
                    {
                        "path": self._relative_path(absolute),
                        "layer": layer,
                        "anomaly": anomaly,
                        "repairable": False,
                        "reason": str(exc),
                    }
                )
                return
            candidates.append(
                {
                    "item_id": uuid.uuid4().hex,
                    "path": self._relative_path(absolute),
                    "type": item_type,
                    "size_bytes": int(snapshot.get("size_bytes") or 0),
                    "fingerprint": snapshot,
                    "layer": layer,
                    "anomaly": anomaly,
                    "reason": reason,
                    "risk": "low",
                    "repairable": True,
                }
            )

        if self.plan_root.is_dir() and not self._is_link_or_reparse_point(
            self.plan_root
        ):
            for path in sorted(self.plan_root.iterdir(), key=lambda item: item.name):
                if self._is_link_or_reparse_point(path) or not path.is_file():
                    continue
                if path.suffix.casefold() == ".tmp":
                    try:
                        age_seconds = current_time - path.stat(
                            follow_symlinks=False
                        ).st_mtime
                    except OSError:
                        continue
                    if age_seconds >= 10 * 60:
                        add_candidate(
                            path,
                            layer="cleaner",
                            anomaly="orphan-atomic-temp",
                            reason="stale atomic-write temporary file",
                        )
                    continue
                if path.suffix.casefold() != ".json":
                    continue
                invalid_reason = ""
                try:
                    document = json.loads(path.read_text(encoding="utf-8"))
                    expires_at = (
                        _parse_iso(str(document.get("expires_at") or ""))
                        if isinstance(document, dict)
                        else None
                    )
                    valid_identity = (
                        isinstance(document, dict)
                        and int(document.get("schema_version") or 0)
                        == PLAN_SCHEMA_VERSION
                        and str(document.get("plan_id") or "") == path.stem
                        and self._is_sha256_text(
                            str(document.get("plan_token") or "")
                        )
                    )
                    if not valid_identity:
                        invalid_reason = "invalid preview-plan document"
                    elif (
                        expires_at is None
                        or expires_at <= datetime.now(timezone.utc)
                    ):
                        invalid_reason = "expired preview plan"
                except (OSError, TypeError, ValueError, json.JSONDecodeError):
                    invalid_reason = "unreadable preview-plan document"
                if invalid_reason:
                    add_candidate(
                        path,
                        layer="cleaner",
                        anomaly="stale-preview-plan",
                        reason=invalid_reason,
                    )

        if include_shared:
            build_stamp = (
                self.project_root
                / "launcher"
                / "state"
                / "production-build.sha256"
            )
            if build_stamp.is_file() and not self._is_link_or_reparse_point(
                build_stamp
            ):
                try:
                    stamp_value = build_stamp.read_text(encoding="ascii")
                except (OSError, UnicodeError):
                    stamp_value = ""
                if not self._is_sha256_text(stamp_value):
                    add_candidate(
                        build_stamp,
                        layer="shared",
                        anomaly="invalid-production-signature",
                        reason=(
                            "invalid production signature; launcher will rebuild "
                            "instead of mixing generations"
                        ),
                    )

            tools_root = self.project_root / "platform_tools"
            if tools_root.is_dir() and not self._is_link_or_reparse_point(
                tools_root
            ):
                for tool_dir in sorted(
                    tools_root.iterdir(),
                    key=lambda item: item.name,
                ):
                    build_root = tool_dir / "build"
                    if (
                        not tool_dir.is_dir()
                        or self._is_link_or_reparse_point(tool_dir)
                        or not build_root.is_dir()
                        or self._is_link_or_reparse_point(build_root)
                    ):
                        continue
                    for lock_path in sorted(
                        build_root.glob(".package-*.lock"),
                        key=lambda item: item.name,
                    ):
                        if (
                            not lock_path.is_dir()
                            or self._is_link_or_reparse_point(lock_path)
                        ):
                            continue
                        try:
                            age_seconds = current_time - lock_path.stat(
                                follow_symlinks=False
                            ).st_mtime
                        except OSError:
                            continue
                        owner: dict[str, Any] = {}
                        owner_path = lock_path / "owner.json"
                        if (
                            owner_path.is_file()
                            and not self._is_link_or_reparse_point(owner_path)
                        ):
                            try:
                                loaded_owner = json.loads(
                                    owner_path.read_text(encoding="utf-8")
                                )
                                if isinstance(loaded_owner, dict):
                                    owner = loaded_owner
                            except (OSError, json.JSONDecodeError):
                                owner = {}
                        owner_pid = owner.get("pid")
                        if isinstance(owner_pid, int) and self._process_is_alive(
                            owner_pid
                        ):
                            continue
                        if age_seconds >= 30:
                            add_candidate(
                                lock_path,
                                layer="package",
                                anomaly="stale-package-lock",
                                reason="package owner process is no longer running",
                            )

                    for recovered_lock in sorted(
                        build_root.glob("package-lock-recovery-*"),
                        key=lambda item: item.name,
                    ):
                        try:
                            age_seconds = current_time - recovered_lock.stat(
                                follow_symlinks=False
                            ).st_mtime
                        except OSError:
                            continue
                        if age_seconds >= 60 * 60:
                            add_candidate(
                                recovered_lock,
                                layer="package",
                                anomaly="retired-package-lock",
                                reason="stale package lock was already retired",
                            )

                    completed: list[Path] = []
                    for package_root in build_root.glob("package-*"):
                        if (
                            not package_root.is_dir()
                            or self._is_link_or_reparse_point(package_root)
                            or not (
                                package_root / "promotion-complete.json"
                            ).is_file()
                            or not (
                                package_root
                                / "promotion-recovery-manifest.json"
                            ).is_file()
                        ):
                            continue
                        aborted_path = package_root / "promotion-aborted.json"
                        if aborted_path.is_file():
                            try:
                                aborted = json.loads(
                                    aborted_path.read_text(encoding="utf-8")
                                )
                            except (OSError, json.JSONDecodeError):
                                aborted = {}
                            if (
                                isinstance(aborted, dict)
                                and aborted.get("status")
                                == "rollback-incomplete"
                            ):
                                continue
                        completed.append(package_root)
                    completed.sort(
                        key=lambda path: path.stat(
                            follow_symlinks=False
                        ).st_mtime_ns,
                        reverse=True,
                    )
                    for excess in completed[1:]:
                        add_candidate(
                            excess,
                            layer="package",
                            anomaly="excess-completed-recovery",
                            reason=(
                                "older successful package recovery exceeds "
                                "the one-generation retention boundary"
                            ),
                        )

        for warning in self.rule_warnings:
            diagnostics.append(
                {
                    "path": self._relative_path(self.rules_override_path)
                    if self._inside_project(self.rules_override_path)
                    else str(self.rules_override_path),
                    "layer": "shared",
                    "anomaly": "rule-warning",
                    "repairable": False,
                    "reason": warning,
                }
            )
        return candidates, diagnostics

    def repair_anomalies(
        self,
        *,
        dry_run: bool = False,
        include_shared: bool = True,
        selected_anomalies: set[str] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        candidates, diagnostics = self._repair_anomaly_candidates(
            include_shared=include_shared,
            now=now,
        )
        if selected_anomalies is not None:
            normalized_selection = {
                str(item).strip()
                for item in selected_anomalies
                if str(item).strip()
            }
            candidates = [
                item
                for item in candidates
                if str(item.get("anomaly") or "") in normalized_selection
            ]
        base_result: dict[str, Any] = {
            "ok": True,
            "dry_run": dry_run,
            "include_shared": include_shared,
            "selected_anomalies": (
                sorted(normalized_selection)
                if selected_anomalies is not None
                else None
            ),
            "anomaly_count": len(candidates) + len(diagnostics),
            "repairable_count": len(candidates),
            "repaired_count": 0,
            "repaired_bytes": 0,
            "permanently_deleted": 0,
            "disk_space_reclaimed_bytes": 0,
            "items": candidates,
            "diagnostics": diagnostics,
            "errors": [],
            "message": (
                f"anomaly scan completed (repairable={len(candidates)}, "
                f"diagnostic={len(diagnostics)})"
            ),
        }
        if dry_run or not candidates:
            return base_result

        with self._mutation_guard("auto-repair") as lock:
            if not lock.get("acquired"):
                return {
                    **base_result,
                    "ok": False,
                    "busy": True,
                    "message": str(
                        lock.get("message") or "project cleaner is busy"
                    ),
                }

            created_at = datetime.now(timezone.utc)
            batch_name = (
                f"repair-{created_at.strftime('%Y%m%d_%H%M%S_%f')}-"
                f"{uuid.uuid4().hex[:8]}"
            )
            batch_dir = self.quarantine_root / batch_name
            batch_dir.mkdir(parents=True, exist_ok=False)
            self._harden_private_path(batch_dir)
            document: dict[str, Any] = {
                "schema_version": QUARANTINE_SCHEMA_VERSION,
                "batch_id": batch_name,
                "status": "applying",
                "created_at": created_at.isoformat(),
                "expires_at": (
                    created_at + timedelta(hours=self._quarantine_ttl_hours())
                ).isoformat(),
                "pinned": False,
                "project_root": str(self.project_root),
                "scope": "auto-repair",
                "plan_id": "",
                "items": [
                    {
                        **item,
                        "original_path": item["path"],
                        "quarantine_path": (
                            Path("items") / str(item["path"])
                        ).as_posix(),
                        "content_sha256": "",
                        "status": "pending",
                    }
                    for item in candidates
                ],
                "errors": [],
                "skipped": [],
            }
            self._write_batch_document(batch_dir, document)

            repaired_count = 0
            repaired_bytes = 0
            errors: list[dict[str, Any]] = []
            skipped: list[dict[str, Any]] = []
            entries = document["items"]
            for index, item in enumerate(candidates):
                relative_path = str(item["path"])
                target = self.project_root / relative_path
                recovery_target = (
                    batch_dir / str(entries[index]["quarantine_path"])
                )
                self._emit_progress(
                    "repair",
                    5 + int(index / max(1, len(candidates)) * 90),
                    "Repairing recoverable project anomaly",
                    current_path=relative_path,
                    completed=index,
                    total=len(candidates),
                )
                if (
                    not target.exists()
                    or self._is_link_or_reparse_point(target)
                ):
                    skip = {
                        "path": relative_path,
                        "reason": "anomaly changed before repair",
                    }
                    skipped.append(skip)
                    entries[index]["status"] = "skipped"
                    entries[index]["status_reason"] = skip["reason"]
                    document["skipped"] = skipped
                    self._write_batch_document(batch_dir, document)
                    continue
                try:
                    current_snapshot = self._candidate_snapshot(
                        target,
                        str(item["type"]),
                    )
                except OSError as exc:
                    errors.append(
                        {"path": relative_path, "message": str(exc)}
                    )
                    entries[index]["status"] = "error"
                    entries[index]["status_reason"] = str(exc)
                    document["errors"] = errors
                    self._write_batch_document(batch_dir, document)
                    continue
                expected = item.get("fingerprint", {})
                if current_snapshot.get("digest") != expected.get("digest"):
                    skip = {
                        "path": relative_path,
                        "reason": "anomaly changed after scan",
                    }
                    skipped.append(skip)
                    entries[index]["status"] = "skipped"
                    entries[index]["status_reason"] = skip["reason"]
                    document["skipped"] = skipped
                    self._write_batch_document(batch_dir, document)
                    continue
                if self._is_locked(target):
                    skip = {
                        "path": relative_path,
                        "reason": "path is in use; cleaner never forces unlock",
                        "locked_by": self._locking_processes(target),
                    }
                    skipped.append(skip)
                    entries[index]["status"] = "locked"
                    entries[index]["locked_by"] = skip["locked_by"]
                    document["skipped"] = skipped
                    self._write_batch_document(batch_dir, document)
                    continue
                try:
                    recovery_target.parent.mkdir(
                        parents=True,
                        exist_ok=True,
                    )
                    os.replace(target, recovery_target)
                    entries[index]["content_sha256"] = self._content_digest(
                        recovery_target,
                        str(item["type"]),
                    )
                    entries[index]["status"] = "moved"
                    repaired_count += 1
                    repaired_bytes += int(item.get("size_bytes") or 0)
                except OSError as exc:
                    errors.append(
                        {"path": relative_path, "message": str(exc)}
                    )
                    entries[index]["status"] = "error"
                    entries[index]["status_reason"] = str(exc)
                document["errors"] = errors
                document["skipped"] = skipped
                self._write_batch_document(batch_dir, document)

            document["status"] = (
                "completed_with_errors" if errors else "completed"
            )
            self._write_batch_document(batch_dir, document)
            manifest_path = batch_dir / "manifest.json"
            if manifest_path.is_file():
                self._atomic_write_json(
                    batch_dir / "tombstone.json",
                    {
                        "schema_version": QUARANTINE_SCHEMA_VERSION,
                        "kind": "repair-tombstone",
                        "status": "recoverable",
                        "created_at": self._iso_now(),
                        "batch_id": batch_name,
                        "scope": "auto-repair",
                        "manifest_path": str(manifest_path),
                        "manifest_sha256": self._file_sha256(manifest_path),
                        "items": [
                            {
                                "original_path": str(
                                    item.get("original_path") or ""
                                ),
                                "recovery_path": str(
                                    item.get("quarantine_path") or ""
                                ),
                                "content_sha256": str(
                                    item.get("content_sha256") or ""
                                ),
                                "type": str(item.get("type") or ""),
                            }
                            for item in entries
                            if str(item.get("status") or "") == "moved"
                        ],
                    },
                )

            result = {
                **base_result,
                "ok": not errors,
                "repaired_count": repaired_count,
                "repaired_bytes": repaired_bytes,
                "quarantine": True,
                "quarantine_path": str(batch_dir),
                "quarantine_manifest": str(manifest_path),
                "items": entries,
                "skipped": skipped,
                "errors": errors,
                "message": (
                    "anomaly repair completed "
                    f"(repaired={repaired_count}, skipped={len(skipped)}, "
                    f"errors={len(errors)})"
                ),
            }
            self._append_history(
                "auto-repair",
                ok=result["ok"],
                scope="global" if include_shared else "runtime",
                item_count=repaired_count,
                bytes=repaired_bytes,
                skipped=len(skipped),
                errors=len(errors),
                batch_id=batch_name,
            )
            self._emit_progress(
                "repair",
                100,
                "Recoverable anomaly repair completed",
                repaired_count=repaired_count,
            )
            return result

    def automatic_cleanup(
        self,
        *,
        force: bool = False,
        requested_scope: str | None = None,
    ) -> dict[str, Any]:
        automation = self.rules.get("automation", {}) if isinstance(self.rules.get("automation"), dict) else {}
        if not bool(automation.get("enabled")) and not force:
            return {"ok": True, "automatic": True, "skipped": True, "message": "automatic cleanup is disabled"}
        scope = str(
            requested_scope
            if force and requested_scope
            else automation.get("scope") or "global"
        )
        normalized_scope, roots = self._candidate_roots(scope)
        if roots is None:
            return {
                "ok": False,
                "automatic": True,
                "scope": scope,
                "message": f"unsupported cleanup scope: {scope}",
            }
        scope = normalized_scope
        disk = shutil.disk_usage(self.project_root)
        free_percent = disk.free / max(1, disk.total) * 100
        threshold = float(automation.get("disk_free_threshold_percent") or 15)
        history = [record for record in self._history_records(limit=MAX_HISTORY_RECORDS) if record.get("action") == "auto-clean"]
        last_at = _parse_iso(str(history[-1].get("timestamp") or "")) if history else None
        interval = max(1, int(automation.get("interval_hours") or 24))
        if not force and free_percent > threshold and last_at and datetime.now(timezone.utc) - last_at < timedelta(hours=interval):
            return {"ok": True, "automatic": True, "skipped": True, "message": "automatic cleanup is not due"}
        repair = self.repair_anomalies(
            include_shared=scope == "global",
        )
        if not repair.get("ok"):
            return {
                "ok": False,
                "automatic": True,
                "scope": scope,
                "repair": repair,
                "message": "automatic cleanup stopped because anomaly repair failed",
            }
        plan = self.plan_cleanup(scope)
        if not plan.get("ok"):
            return {**plan, "automatic": True, "repair": repair}
        max_bytes = max(1, int(automation.get("max_bytes_per_run") or 512 * 1024 * 1024))
        selected: list[str] = []
        selected_bytes = 0
        for item in sorted(plan.get("items", []), key=lambda entry: int(entry.get("size_bytes") or 0), reverse=True):
            if str(item.get("risk")) != "low":
                continue
            size = int(item.get("size_bytes") or 0)
            if selected and selected_bytes + size > max_bytes:
                continue
            selected.append(str(item.get("item_id")))
            selected_bytes += size
        if not selected:
            result = {
                "ok": True,
                "automatic": True,
                "skipped": True,
                "scope": scope,
                "cleaned_files": 0,
                "cleaned_dirs": 0,
                "cleaned_bytes": 0,
                "message": "automatic cleanup found no low-risk items",
            }
        else:
            result = self.cleanup_garbage(
                scope,
                quarantine=True,
                plan_id=str(plan.get("plan_id") or ""),
                plan_token=str(plan.get("plan_token") or ""),
                selected_item_ids=selected,
            )
            result["automatic"] = True
        result["repair"] = repair
        result["repaired_anomalies"] = int(
            repair.get("repaired_count") or 0
        )
        self._append_history("auto-clean", ok=bool(result.get("ok")), scope=scope, bytes=int(result.get("cleaned_bytes") or 0), item_count=int(result.get("cleaned_files") or 0) + int(result.get("cleaned_dirs") or 0))
        return result

    def _system_rescue_subprocess(
        self,
        command: list[str],
        *,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        environment = os.environ.copy()
        environment.pop("ELECTRON_RUN_AS_NODE", None)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        try:
            completed = subprocess.run(
                command,
                cwd=str(self.project_root),
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
                **(
                    {
                        "creationflags": getattr(
                            subprocess,
                            "CREATE_NO_WINDOW",
                            0,
                        )
                    }
                    if os.name == "nt"
                    else {}
                ),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {
                "ok": False,
                "exit_code": 124
                if isinstance(exc, subprocess.TimeoutExpired)
                else -1,
                "output": str(exc),
            }
        return {
            "ok": completed.returncode == 0,
            "exit_code": completed.returncode,
            "output": completed.stdout[-12000:],
        }

    def _system_rescue_package_check(self) -> dict[str, Any]:
        script = self.project_root / "scripts" / "package_platform_tools.py"
        if (
            not script.is_file()
            or self._is_link_or_reparse_point(script)
            or not self._inside_project(script)
        ):
            return {
                "ok": False,
                "available": False,
                "message": "package verification entry is unavailable",
                "results": [],
            }
        run = self._system_rescue_subprocess(
            [
                sys.executable,
                "-B",
                "-s",
                str(script),
                "--all",
                "--verify",
                "--json",
            ],
            timeout_seconds=180,
        )
        payload: dict[str, Any] = {}
        for line in reversed(str(run.get("output") or "").splitlines()):
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, dict):
                payload = loaded
                break
        results = payload.get("results", [])
        if not isinstance(results, list):
            results = []
        hot_update_eligible = bool(results) and all(
            isinstance(item, dict)
            and (
                item.get("ok") is True
                or item.get("error_code") == "STALE_PACKAGE"
            )
            for item in results
        )
        package_ok = (
            bool(run.get("ok")) and payload.get("ok") is True
        ) or hot_update_eligible
        return {
            "ok": package_ok,
            "available": True,
            "exit_code": run.get("exit_code"),
            "results": results,
            "hot_update_eligible": hot_update_eligible,
            "upgrade_strategy": (
                "direct-source-hot-update"
                if hot_update_eligible
                else "packaged-baseline"
            ),
            "message": (
                "all packaged tools are current"
                if bool(run.get("ok")) and payload.get("ok") is True
                else "source changes are eligible for direct hot update"
                if hot_update_eligible
                else "one or more packaged tools require a verified upgrade"
            ),
            "output": str(run.get("output") or "")[-4000:],
        }

    @staticmethod
    def _system_rescue_main_health() -> dict[str, Any]:
        try:
            with urllib_request.urlopen(
                "http://127.0.0.1:8765/health",
                timeout=2,
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (
            OSError,
            TimeoutError,
            UnicodeError,
            json.JSONDecodeError,
            urllib_error.URLError,
        ) as exc:
            return {
                "ok": False,
                "reachable": False,
                "message": f"main backend is unavailable: {exc}",
            }
        return {
            "ok": payload.get("ok") is True,
            "reachable": True,
            "runtime_state": payload.get("runtime_state"),
            "phase": payload.get("phase"),
            "workspace_instance_id": payload.get("workspace_instance_id"),
            "message": "main backend health endpoint responded",
        }

    def system_rescue_check(self, *, deep: bool = False) -> dict[str, Any]:
        """Run bounded system diagnostics owned by Project Cleaner."""

        self._emit_progress("rescue", 5, "Checking project boundary")
        required: list[dict[str, Any]] = []
        for relative_path in SYSTEM_RESCUE_REQUIRED_PATHS:
            target = self.project_root / relative_path
            valid = False
            reason = ""
            try:
                valid = (
                    self._inside_project(target)
                    and target.is_file()
                    and not self._is_link_or_reparse_point(target)
                )
                if not valid:
                    reason = "missing, non-regular, or outside project boundary"
            except OSError as exc:
                reason = str(exc)
            required.append(
                {
                    "path": relative_path,
                    "ok": valid,
                    "reason": reason,
                }
            )

        self._emit_progress("rescue", 25, "Validating system configuration")
        configurations: list[dict[str, Any]] = []
        for relative_path in (
            "package.json",
            "config/settings.json",
            "config/tool-runtime-contract.json",
        ):
            target = self.project_root / relative_path
            valid = False
            reason = ""
            try:
                loaded = json.loads(target.read_text(encoding="utf-8"))
                valid = isinstance(loaded, dict)
                if not valid:
                    reason = "configuration root must be an object"
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                reason = str(exc)
            configurations.append(
                {
                    "path": relative_path,
                    "ok": valid,
                    "reason": reason,
                }
            )

        self._emit_progress("rescue", 45, "Checking package integrity")
        packages = self._system_rescue_package_check()
        anomalies = self.repair_anomalies(
            dry_run=True,
            include_shared=True,
            selected_anomalies=set(SYSTEM_RESCUE_REPAIR_ANOMALIES),
        )
        environment = {
            "python": {
                "ok": Path(sys.executable).is_file(),
                "path": sys.executable,
            },
            "node": {
                "ok": shutil.which("node") is not None,
                "path": shutil.which("node") or "",
            },
            "npm": {
                "ok": shutil.which("npm.cmd" if os.name == "nt" else "npm")
                is not None,
                "path": shutil.which(
                    "npm.cmd" if os.name == "nt" else "npm"
                )
                or "",
            },
        }
        main_health = self._system_rescue_main_health()
        typecheck: dict[str, Any] = {
            "ok": True,
            "skipped": True,
            "message": "deep type check was not requested",
        }
        if deep:
            self._emit_progress("rescue", 70, "Running deep type check")
            npm = "npm.cmd" if os.name == "nt" else "npm"
            typecheck = self._system_rescue_subprocess(
                [npm, "run", "type-check"],
                timeout_seconds=180,
            )
            typecheck["skipped"] = False
            typecheck["message"] = (
                "type check passed"
                if typecheck.get("ok")
                else "type check failed"
            )

        blocking: list[str] = []
        blocking.extend(
            f"required:{item['path']}"
            for item in required
            if not item["ok"]
        )
        blocking.extend(
            f"config:{item['path']}"
            for item in configurations
            if not item["ok"]
        )
        if not packages.get("ok"):
            blocking.append("package-integrity")
        if not typecheck.get("ok"):
            blocking.append("type-check")
        repairable_count = int(anomalies.get("repairable_count") or 0)
        state = (
            "blocked"
            if blocking
            else "repairable"
            if repairable_count
            else "healthy"
        )
        result = {
            "ok": not blocking,
            "operation": "system-rescue-check",
            "state": state,
            "deep": deep,
            "project_root": str(self.project_root),
            "authority": "project-cleaner",
            "boundary": "project-only",
            "required_paths": required,
            "configurations": configurations,
            "environment": environment,
            "packages": packages,
            "main_health": main_health,
            "typecheck": typecheck,
            "anomalies": anomalies,
            "repairable_count": repairable_count,
            "blocking": blocking,
            "message": (
                "system rescue check passed"
                if state == "healthy"
                else "system rescue found recoverable anomalies"
                if state == "repairable"
                else "system rescue found blocking problems"
            ),
        }
        self._append_history(
            "system-rescue-check",
            ok=result["ok"],
            scope="global",
            item_count=repairable_count,
            errors=len(blocking),
        )
        self._emit_progress(
            "rescue",
            100,
            "System rescue check completed",
            state=state,
        )
        return result

    def system_rescue_repair(self, *, deep: bool = False) -> dict[str, Any]:
        """Repair only Project Cleaner-owned anomalies, then re-diagnose."""

        before = self.system_rescue_check(deep=False)
        self._emit_progress("rescue-repair", 35, "Applying safe rescue repair")
        repair = self.repair_anomalies(
            dry_run=False,
            include_shared=True,
            selected_anomalies=set(SYSTEM_RESCUE_REPAIR_ANOMALIES),
        )
        after = self.system_rescue_check(deep=deep)
        unresolved = int(after.get("repairable_count") or 0)
        ok = bool(repair.get("ok")) and bool(after.get("ok")) and unresolved == 0
        result = {
            "ok": ok,
            "operation": "system-rescue-repair",
            "state": "healthy" if ok else "blocked",
            "project_root": str(self.project_root),
            "authority": "project-cleaner",
            "boundary": "project-only",
            "before": before,
            "repair": repair,
            "after": after,
            "repaired_count": int(repair.get("repaired_count") or 0),
            "unresolved_count": unresolved,
            "quarantine_path": str(repair.get("quarantine_path") or ""),
            "message": (
                "system rescue repair completed"
                if ok
                else "system rescue stopped with unresolved problems"
            ),
        }
        self._append_history(
            "system-rescue-repair",
            ok=ok,
            scope="global",
            item_count=int(repair.get("repaired_count") or 0),
            errors=len(repair.get("errors") or []),
            batch_id=Path(str(repair.get("quarantine_path") or "")).name,
        )
        self._emit_progress(
            "rescue-repair",
            100,
            "System rescue repair completed",
            state=result["state"],
        )
        return result

    def get_status(self) -> dict[str, Any]:
        quarantine = self.list_quarantine_batches()
        repair_items, repair_diagnostics = self._repair_anomaly_candidates(
            include_shared=True,
        )
        disk = shutil.disk_usage(self.project_root)
        return {
            "ok": True,
            "version": self.VERSION,
            "project_root": str(self.project_root),
            "supported_scopes": ["global", "runtime", "sandbox"],
            "default_scope": "runtime",
            "quarantine_root": str(self.quarantine_root),
            "recovery_root": str(self.recovery_root),
            "managed_storage": {
                "authority": "project-cleaner",
                "data_root": str(self.data_root),
                "backup_root": str(self.backup_root),
                "audit_root": str(self.audit_root),
                "logs_root": str(self.logs_root),
                "backup_max_records": 1,
            },
            "quarantine": quarantine,
            "quarantine_health": quarantine.get("health", {}),
            "history": list(reversed(self._history_records(limit=50))),
            "rules": {
                "schema_version": self.rules.get("schema_version"),
                "override_path": str(self.rules_override_path),
                "override_exists": self.rules_override_path.exists(),
                "directory_rule_count": len(self.rules.get("directory_rules", [])),
                "file_rule_count": len(self.rules.get("file_rules", [])),
                "warnings": self.rule_warnings,
            },
            "automation": self.rules.get("automation", {}),
            "anomalies": {
                "repairable_count": len(repair_items),
                "diagnostic_count": len(repair_diagnostics),
                "items": repair_items,
                "diagnostics": repair_diagnostics,
            },
            "disk": {
                "total_bytes": disk.total,
                "used_bytes": disk.used,
                "free_bytes": disk.free,
                "free_percent": round(disk.free / max(1, disk.total) * 100, 2),
            },
            "safety": {
                "preview_plan_required": True,
                "plan_signature": "HMAC-SHA256",
                "git_protection": True,
                "never_force_unlock": True,
                "transactional_quarantine": True,
                "permanent_delete": False,
                "sha256_recovery_manifests": True,
                "tombstones": True,
            },
            "capabilities": {
                "mutation_root": str(self.project_root),
                "read_only": [
                    "status",
                    "preview",
                    "storage-analysis",
                    "anomaly-diagnosis",
                ],
                "recoverable_mutation": [
                    "low-risk-cleanup",
                    "anomaly-repair",
                    "quarantine",
                    "restore",
                ],
                "maintenance_layers": [
                    "cleaner",
                    "shared",
                    "package",
                ],
                "forbidden": [
                    "outside-project",
                    "source-code-edit",
                    "git-tracked-file",
                    "force-unlock",
                    "process-termination",
                    "active-package-lock",
                    "current-dist",
                    "rollback-incomplete",
                    "dependency-directory",
                    "browser-profile",
                    "user-data",
                ],
            },
            "message": "project cleaner status ready",
        }
