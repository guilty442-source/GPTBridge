from __future__ import annotations

import ctypes
import hashlib
import json
import os
import stat as stat_module
import subprocess
import threading
import uuid
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Iterator

from .business_history import BusinessHistoryStore
from .cleanup_constants import (
    BACKUP_EXTRACT_RELATIVE_ROOT,
    CLEANER_RUNTIME_RELATIVE_PATH,
    CORE_EXCLUDED_DIRECTORY_NAMES,
    CORE_PROTECTED_RELATIVE_PATHS,
    FALLBACK_RULES,
    MANAGED_BACKUP_RELATIVE_ROOT,
    MAX_REPORTED_SKIPS,
    PLAN_SCHEMA_VERSION,
    ProgressCallback,
    QUARANTINE_RELATIVE_PATH,
    RECOVERY_RELATIVE_PATH,
    SECONDS_PER_DAY,
    _background_subprocess_kwargs,
    _deep_merge,
    _PROCESS_LOCKS,
    _PROCESS_LOCKS_GUARD,
)


class CoreMixin:
    """Core infrastructure: constructor, path safety, locking, persistence."""

    def __init__(
        self,
        project_root: Path,
        *,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.runtime_root = self.project_root / CLEANER_RUNTIME_RELATIVE_PATH
        self.data_root = self.runtime_root / "business"
        self.backup_root = self.project_root / MANAGED_BACKUP_RELATIVE_ROOT
        self.backup_extract_root = (
            self.project_root / BACKUP_EXTRACT_RELATIVE_ROOT
        )
        self.quarantine_root = self.project_root / QUARANTINE_RELATIVE_PATH
        self.recovery_root = self.project_root / RECOVERY_RELATIVE_PATH
        self.plan_root = self.runtime_root / "plans"
        self.business_history = BusinessHistoryStore(
            self.data_root / "history.sqlite3",
        )
        self.key_path = self.runtime_root / "plan.key"
        self.rules_override_path = self.project_root / "config" / "global-cleaner-rules.json"
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
        bundled_path = Path(__file__).resolve().parent.parent / "domain" / "cleanup_rules.json"
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
                        for field in ("analysis",)
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
        self.business_history.append(action, **payload)

    def _history_records(self, *, limit: int = 50) -> list[dict[str, Any]]:
        return self.business_history.read(limit=limit)

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
