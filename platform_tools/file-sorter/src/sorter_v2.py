"""Durable, no-overwrite file operations and per-target state for File Sorter.

The module intentionally has no dependency on ``main.py``.  This keeps the
transaction and profile repository usable by a future background service.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat as stat_module
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


STATE_ROOT_ENV = "FILE_SORTER_STATE_ROOT"
SCHEMA_VERSION = 2
DEFAULT_QUIET_SECONDS = 2.0
DEFAULT_PLAN_TTL_SECONDS = 15 * 60
DEFAULT_INCLUDE = ("*",)
DEFAULT_EXCLUDE: tuple[str, ...] = ()
PARTIAL_SUFFIXES = (
    ".crdownload",
    ".download",
    ".partial",
    ".part",
    ".tmp",
    ".temp",
    ".opdownload",
)
TERMINAL_TRANSACTION_STATES = {
    "committed",
    "undone",
    "undo_failed",
}
_SAFE_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{7,127}$")
_TARGET_LOCKS_GUARD = threading.RLock()
_TARGET_LOCKS_HELD: set[str] = set()


class SorterV2Error(Exception):
    """Base error for durable sorter operations."""


class RuleConflictError(SorterV2Error):
    """Raised when a profile revision changed during an update."""


@dataclass(frozen=True)
class FileFingerprint:
    size: int
    mtime_ns: int

    def to_dict(self) -> dict[str, int]:
        return {"size": self.size, "mtime_ns": self.mtime_ns}


@dataclass(frozen=True)
class _FileMetadata:
    """Metadata that must survive a staged, cross-volume transfer."""

    mtime_ns: int
    permissions: int
    file_attributes: int | None
    alternate_streams: tuple[tuple[str, int, str], ...] | None
    extended_attributes: tuple[tuple[str, str], ...] | None


@dataclass(frozen=True)
class StabilityResult:
    stable: bool
    reason: str | None
    fingerprint: FileFingerprint | None


@dataclass
class PlanOperation:
    operation_id: str
    source: str
    destination: str
    keyword: str
    folder: str
    rule_source: str
    source_size: int
    source_mtime_ns: int
    transfer: str
    status: str = "ready"
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "source": self.source,
            "destination": self.destination,
            "keyword": self.keyword,
            "folder": self.folder,
            "rule_source": self.rule_source,
            "source_size": self.source_size,
            "source_mtime_ns": self.source_mtime_ns,
            "transfer": self.transfer,
            "status": self.status,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PlanOperation":
        return cls(
            operation_id=str(value["operation_id"]),
            source=str(value["source"]),
            destination=str(value["destination"]),
            keyword=str(value.get("keyword", "")),
            folder=str(value.get("folder", "")),
            rule_source=str(value.get("rule_source", "custom")),
            source_size=int(value["source_size"]),
            source_mtime_ns=int(value["source_mtime_ns"]),
            transfer=str(value.get("transfer", "unknown")),
            status=str(value.get("status", "ready")),
            reason=(
                None
                if value.get("reason") is None
                else str(value.get("reason"))
            ),
        )


@dataclass
class SkippedFile:
    source: str
    category: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {
            "source": self.source,
            "category": self.category,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SkippedFile":
        return cls(
            source=str(value["source"]),
            category=str(value["category"]),
            reason=str(value["reason"]),
        )


@dataclass
class OrganizePlan:
    plan_id: str
    target_dir: str
    profile_id: str
    rules_revision: int
    quiet_seconds: float
    created_at: str = field(default_factory=lambda: _utc_now())
    expires_at: str = field(
        default_factory=lambda: _utc_after(DEFAULT_PLAN_TTL_SECONDS)
    )
    operations: list[PlanOperation] = field(default_factory=list)
    skipped: list[SkippedFile] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for item in self.skipped:
            counts[item.category] = counts.get(item.category, 0) + 1
        return {
            "ok": True,
            "type": "file-sorter-plan",
            "schema_version": SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "target_dir": self.target_dir,
            "profile_id": self.profile_id,
            "rules_revision": self.rules_revision,
            "quiet_seconds": self.quiet_seconds,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "operations": [item.to_dict() for item in self.operations],
            "skipped": [item.to_dict() for item in self.skipped],
            "summary": {
                "ready": len(self.operations),
                "skipped": len(self.skipped),
                "unmatched": counts.get("unmatched", 0),
                "unstable": counts.get("unstable", 0),
                "filtered": counts.get("filtered", 0),
            },
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OrganizePlan":
        created_at = str(value.get("created_at", _utc_now()))
        return cls(
            plan_id=str(value["plan_id"]),
            target_dir=str(value["target_dir"]),
            profile_id=str(value["profile_id"]),
            rules_revision=int(value.get("rules_revision", 0)),
            quiet_seconds=float(value.get("quiet_seconds", 0.0)),
            created_at=created_at,
            expires_at=str(
                value.get("expires_at")
                or _utc_after(DEFAULT_PLAN_TTL_SECONDS, base=created_at)
            ),
            operations=[
                PlanOperation.from_dict(item)
                for item in value.get("operations", [])
            ],
            skipped=[
                SkippedFile.from_dict(item)
                for item in value.get("skipped", [])
            ],
        )


@dataclass(frozen=True)
class ProfileSnapshot:
    profile_id: str
    profile_name: str | None
    target_dir: str
    revision: int
    enabled: bool
    quiet_seconds: float
    include: tuple[str, ...]
    exclude: tuple[str, ...]
    rules: tuple[dict[str, str], ...]
    path: Path
    migrated_from: str | None = None
    migration_required_review: bool = False
    migration_rejected_rule_count: int = 0

    def to_dict(self, *, include_rules: bool = True) -> dict[str, Any]:
        value: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "profile_id": self.profile_id,
            "profile_name": self.profile_name,
            "target": self.target_dir,
            "target_dir": self.target_dir,
            "revision": self.revision,
            "enabled": self.enabled,
            "quiet_seconds": self.quiet_seconds,
            "include": list(self.include),
            "exclude": list(self.exclude),
            "path": str(self.path),
            "migrated_from": self.migrated_from,
            "migration_required_review": self.migration_required_review,
            "migration_rejected_rule_count": self.migration_rejected_rule_count,
        }
        if include_rules:
            value["rules"] = [dict(item) for item in self.rules]
        return value


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utc_after(seconds: float, *, base: str | None = None) -> str:
    if base:
        try:
            base_time = datetime.fromisoformat(base.replace("Z", "+00:00"))
            if base_time.tzinfo is None:
                base_time = base_time.replace(tzinfo=timezone.utc)
        except ValueError:
            base_time = datetime.now(timezone.utc)
    else:
        base_time = datetime.now(timezone.utc)
    return datetime.fromtimestamp(
        base_time.timestamp() + max(0.0, seconds),
        timezone.utc,
    ).isoformat()


def _plan_expired(plan: OrganizePlan) -> bool:
    try:
        expires_at = datetime.fromisoformat(plan.expires_at.replace("Z", "+00:00"))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (AttributeError, ValueError):
        return True
    return expires_at.astimezone(timezone.utc) <= datetime.now(timezone.utc)


def resolve_state_root(explicit: str | Path | None = None) -> Path:
    """Return the user-writable state root, without creating it."""

    if explicit is not None:
        return Path(os.path.abspath(os.path.normpath(str(Path(explicit).expanduser()))))
    configured = os.environ.get(STATE_ROOT_ENV, "").strip()
    if configured:
        return Path(
            os.path.abspath(os.path.normpath(str(Path(configured).expanduser())))
        )
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return Path(
            os.path.abspath(
                os.path.normpath(str(base / "GPTBridge" / "file-sorter"))
            )
        )
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    base = Path(xdg_state) if xdg_state else Path.home() / ".local" / "state"
    return Path(
        os.path.abspath(os.path.normpath(str(base / "GPTBridge" / "file-sorter")))
    )


def profile_id_for(target_dir: str | Path, profile: str | None = None) -> str:
    target = Path(target_dir).expanduser().resolve()
    normalized = os.path.normcase(str(target))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]
    if not profile:
        return f"target-{digest}"
    slug = re.sub(r"[^a-zA-Z0-9_.-]+", "-", profile.strip()).strip(".-")
    if not slug:
        raise SorterV2Error("Profile name must contain a letter or number.")
    name_digest = hashlib.sha256(profile.strip().encode("utf-8")).hexdigest()[:10]
    return f"{slug[:37]}-{name_digest}-{digest}"


def profile_path(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
) -> Path:
    profile_id = profile_id_for(target_dir, profile)
    return resolve_state_root(state_root) / "profiles" / profile_id / "profile.json"


def _lock_owner_alive(path: Path) -> bool | None:
    """Compatibility diagnostic; lock acquisition never trusts this result."""

    try:
        raw_pid = path.read_text(encoding="ascii").split(":", 1)[0]
        pid = int(raw_pid)
    except (OSError, UnicodeError, ValueError):
        return None
    if pid == os.getpid():
        return True
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_process_alive(pid: int) -> bool | None:
    """Query a Windows process without sending it a signal."""

    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        open_process = kernel32.OpenProcess
        open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        open_process.restype = wintypes.HANDLE
        get_exit_code = kernel32.GetExitCodeProcess
        get_exit_code.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_exit_code.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = open_process(0x1000, False, pid)
        if not handle:
            error_code = ctypes.get_last_error()
            if error_code == 5:
                return True
            if error_code == 87:
                return False
            return None
        try:
            exit_code = wintypes.DWORD()
            if not get_exit_code(handle, ctypes.byref(exit_code)):
                return None
            return exit_code.value == 259  # STILL_ACTIVE
        finally:
            close_handle(handle)
    except (AttributeError, ImportError, OSError, ValueError):
        return None


def _try_lock_descriptor(descriptor: int) -> bool:
    """Acquire a process-scoped OS lock; lock-file contents are diagnostic only."""

    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def _unlock_descriptor(descriptor: int) -> None:
    os.lseek(descriptor, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return

    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


class _ExclusiveFileLock:
    def __init__(
        self,
        path: Path,
        *,
        timeout_seconds: float = 5.0,
        stale_seconds: float = 30.0,
    ) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.stale_seconds = stale_seconds
        self.token = f"{os.getpid()}:{uuid.uuid4().hex}"
        self.acquired = False
        self._descriptor: int | None = None

    def __enter__(self) -> "_ExclusiveFileLock":
        if self.acquired:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout_seconds
        payload = (self.token + "\n").encode("ascii")
        while True:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_RDWR,
                0o600,
            )
            descriptor_locked = False
            try:
                descriptor_locked = _try_lock_descriptor(descriptor)
                if not descriptor_locked:
                    os.close(descriptor)
                    if time.monotonic() >= deadline:
                        raise SorterV2Error(
                            f"Timed out waiting for state lock: {self.path}"
                        )
                    time.sleep(0.05)
                    continue
                os.lseek(descriptor, 0, os.SEEK_SET)
                os.write(descriptor, payload)
                os.ftruncate(descriptor, len(payload))
                os.fsync(descriptor)
                _fsync_directory(self.path.parent)
                self._descriptor = descriptor
                self.acquired = True
                return self
            except Exception:
                try:
                    if descriptor_locked:
                        _unlock_descriptor(descriptor)
                except OSError:
                    pass
                finally:
                    if self._descriptor != descriptor:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
                raise

    def __exit__(self, *_: object) -> None:
        if not self.acquired:
            return
        descriptor = self._descriptor
        self._descriptor = None
        self.acquired = False
        if descriptor is None:
            return
        try:
            _unlock_descriptor(descriptor)
        finally:
            os.close(descriptor)


class _TargetDirectoryLock:
    """Cross-process lock keyed only by a canonical target, without writing it."""

    def __init__(
        self,
        target_dir: str | Path,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        self.target = _validated_target_directory(target_dir)
        self.timeout_seconds = max(0.0, timeout_seconds)
        initial = self.target.stat()
        self._identity = (initial.st_dev, initial.st_ino)
        self._key = os.path.normcase(str(self.target))
        self._registered = False
        self._descriptor: int | None = None
        self._mutex_handle: Any | None = None

    def __enter__(self) -> "_TargetDirectoryLock":
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            create_mutex = kernel32.CreateMutexW
            create_mutex.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
            create_mutex.restype = wintypes.HANDLE
            wait_for_single = kernel32.WaitForSingleObject
            wait_for_single.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            wait_for_single.restype = wintypes.DWORD
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            normalized = os.path.normcase(str(self.target))
            digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
            handle = create_mutex(
                None,
                False,
                f"Local\\GPTBridge.FileSorter.Target.{digest}",
            )
            if not handle:
                raise SorterV2Error(
                    f"Unable to create target lock: {self.target}"
                )
            timeout_ms = min(
                int(self.timeout_seconds * 1000),
                0xFFFFFFFE,
            )
            result = int(wait_for_single(handle, timeout_ms))
            if result not in {0x00000000, 0x00000080}:
                close_handle(handle)
                if result == 0x00000102:
                    raise SorterV2Error(
                        f"Timed out waiting for target lock: {self.target}"
                    )
                raise SorterV2Error(
                    f"Unable to acquire target lock: {self.target}"
                )
            self._mutex_handle = handle
        else:
            import fcntl

            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            descriptor = os.open(self.target, flags)
            deadline = time.monotonic() + self.timeout_seconds
            while True:
                try:
                    fcntl.flock(
                        descriptor,
                        fcntl.LOCK_EX | fcntl.LOCK_NB,
                    )
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        os.close(descriptor)
                        raise SorterV2Error(
                            f"Timed out waiting for target lock: {self.target}"
                        )
                    time.sleep(0.05)
            self._descriptor = descriptor

        with _TARGET_LOCKS_GUARD:
            if self._key in _TARGET_LOCKS_HELD:
                self.__exit__()
                raise SorterV2Error(
                    f"Timed out waiting for target lock: {self.target}"
                )
            _TARGET_LOCKS_HELD.add(self._key)
            self._registered = True

        current = _validated_target_directory(self.target).stat()
        if (current.st_dev, current.st_ino) != self._identity:
            self.__exit__()
            raise SorterV2Error(
                f"Target changed identity while acquiring its lock: {self.target}"
            )
        return self

    def __exit__(self, *_: object) -> None:
        if self._registered:
            with _TARGET_LOCKS_GUARD:
                _TARGET_LOCKS_HELD.discard(self._key)
            self._registered = False
        if self._mutex_handle is not None:
            import ctypes
            from ctypes import wintypes

            handle = self._mutex_handle
            self._mutex_handle = None
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            release_mutex = kernel32.ReleaseMutex
            release_mutex.argtypes = [wintypes.HANDLE]
            release_mutex.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            try:
                release_mutex(handle)
            finally:
                close_handle(handle)
        if self._descriptor is not None:
            import fcntl

            descriptor = self._descriptor
            self._descriptor = None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    created = False
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            created = True
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        created = False
        _fsync_directory(path.parent)
    finally:
        if created:
            temporary.unlink(missing_ok=True)


def _clean_patterns(
    values: Iterable[object] | None,
    *,
    default: Sequence[str],
) -> tuple[str, ...]:
    if values is None:
        return tuple(default)
    cleaned = tuple(str(item).strip() for item in values if str(item).strip())
    return cleaned


def _clean_rule_dicts(
    rules: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, str], ...]:
    cleaned: list[dict[str, str]] = []
    for item in rules:
        keyword = str(item.get("keyword", "")).strip()
        folder = str(item.get("folder", "")).strip()
        if not keyword or not folder:
            continue
        folder_path = Path(folder).expanduser()
        if (
            folder_path.is_absolute()
            or folder in {".", ".."}
            or folder_path.name != folder
            or "/" in folder
            or "\\" in folder
        ):
            raise SorterV2Error(
                "Profile destination rules must name one direct child folder."
            )
        cleaned.append({"keyword": keyword, "folder": folder})
    return tuple(cleaned)


def _is_link_or_reparse(path: Path) -> bool:
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return False
    except OSError as error:
        raise SorterV2Error(f"Cannot safely inspect path {path}: {error}") from error
    attributes = int(getattr(path_stat, "st_file_attributes", 0) or 0)
    return path.is_symlink() or bool(attributes & 0x400)


def _same_path_identity(left: Path, right: Path) -> bool:
    return os.path.normcase(str(left)) == os.path.normcase(str(right))


def _validated_target_directory(
    target_dir: str | Path,
    *,
    label: str = "Target directory",
) -> Path:
    requested = Path(target_dir).expanduser()
    if not requested.is_absolute():
        raise SorterV2Error(f"{label} must be absolute: {requested}")
    try:
        canonical = requested.resolve(strict=True)
    except OSError as error:
        raise SorterV2Error(f"{label} cannot be resolved: {requested}: {error}") from error
    if not _same_path_identity(requested, canonical):
        raise SorterV2Error(f"{label} must be canonical: {requested}")
    if _is_link_or_reparse(requested):
        raise SorterV2Error(f"{label} cannot be a link or reparse point: {requested}")
    if not canonical.is_dir():
        raise SorterV2Error(f"{label} does not exist: {canonical}")
    return canonical


def _state_category_root(
    state_root: str | Path | None,
    category: str,
) -> Path:
    root = resolve_state_root(state_root)
    existing_ancestor = root
    while (
        not existing_ancestor.exists()
        and not existing_ancestor.is_symlink()
        and existing_ancestor != existing_ancestor.parent
    ):
        existing_ancestor = existing_ancestor.parent
    if (
        _is_link_or_reparse(existing_ancestor)
        or not existing_ancestor.is_dir()
        or not _same_path_identity(
            existing_ancestor,
            existing_ancestor.resolve(strict=True),
        )
    ):
        raise SorterV2Error(f"Invalid state root boundary: {root}")
    if root.exists():
        if (
            _is_link_or_reparse(root)
            or not root.is_dir()
            or not _same_path_identity(root, root.resolve(strict=True))
        ):
            raise SorterV2Error(f"Invalid state root: {root}")
    category_root = root / category
    if category_root.exists():
        if _is_link_or_reparse(category_root) or not category_root.is_dir():
            raise SorterV2Error(
                f"State category cannot be a link or reparse point: {category_root}"
            )
        canonical = category_root.resolve(strict=True)
        if not _same_path_identity(category_root, canonical):
            raise SorterV2Error(f"State category escaped its root: {category_root}")
    return category_root


def _validated_state_document_path(
    path: Path,
    *,
    state_root: str | Path | None,
    category: str,
    relative_parts: int,
    require_exists: bool,
) -> Path:
    category_root = _state_category_root(state_root, category)
    requested = Path(path)
    if not requested.is_absolute():
        raise SorterV2Error(f"State document must be absolute: {requested}")
    try:
        relative = requested.relative_to(category_root)
    except ValueError as error:
        raise SorterV2Error(
            f"State document escaped the {category} directory: {requested}"
        ) from error
    if len(relative.parts) != relative_parts or any(
        part in {"", ".", ".."} for part in relative.parts
    ):
        raise SorterV2Error(f"Invalid {category} state path: {requested}")

    current = category_root
    for part in relative.parts[:-1]:
        current = current / part
        if current.exists() or current.is_symlink():
            if _is_link_or_reparse(current) or not current.is_dir():
                raise SorterV2Error(
                    f"State path cannot use a link or reparse point: {current}"
                )
            canonical_parent = current.resolve(strict=True)
            if not _same_path_identity(current, canonical_parent):
                raise SorterV2Error(f"State path escaped its root: {current}")

    exists_or_link = requested.exists() or requested.is_symlink()
    if require_exists and not exists_or_link:
        raise SorterV2Error(f"State document does not exist: {requested}")
    if exists_or_link:
        if _is_link_or_reparse(requested):
            raise SorterV2Error(
                f"State document cannot be a link or reparse point: {requested}"
            )
        try:
            canonical = requested.resolve(strict=True)
        except OSError as error:
            raise SorterV2Error(
                f"Cannot safely resolve state document {requested}: {error}"
            ) from error
        if not _same_path_identity(requested, canonical):
            raise SorterV2Error(f"State document escaped its root: {requested}")
        if not canonical.is_file():
            raise SorterV2Error(f"State document is not a regular file: {requested}")
    return requested


def _read_profile_document(
    path: Path,
    *,
    state_root: str | Path | None,
) -> ProfileSnapshot:
    path = _validated_state_document_path(
        path,
        state_root=state_root,
        category="profiles",
        relative_parts=2,
        require_exists=True,
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SorterV2Error(f"Cannot read profile {path}: {error}") from error
    if not isinstance(value, dict):
        raise SorterV2Error(f"Invalid profile document: {path}")
    target_dir = str(value.get("target_dir") or value.get("target") or "").strip()
    profile_id = str(value.get("profile_id", path.parent.name)).strip()
    profile_name = (
        str(value["profile_name"])
        if value.get("profile_name") is not None
        else None
    )
    raw_rules = value.get("rules", [])
    if not target_dir or not isinstance(raw_rules, list):
        raise SorterV2Error(f"Invalid profile document: {path}")
    raw_target = Path(target_dir).expanduser()
    if not raw_target.is_absolute():
        raise SorterV2Error(f"Profile target is not absolute: {path}")
    canonical_target = _validated_target_directory(
        raw_target,
        label=f"Profile target in {path}",
    )
    expected_profile_id = profile_id_for(canonical_target, profile_name)
    if (
        path.name != "profile.json"
        or path.parent.name != profile_id
        or profile_id != expected_profile_id
    ):
        raise SorterV2Error(f"Profile identity does not match its state path: {path}")
    return ProfileSnapshot(
        profile_id=profile_id,
        profile_name=profile_name,
        target_dir=str(canonical_target),
        revision=max(0, int(value.get("revision", 0))),
        enabled=bool(value.get("enabled", False)),
        quiet_seconds=max(0.0, float(value.get("quiet_seconds", DEFAULT_QUIET_SECONDS))),
        include=_clean_patterns(value.get("include"), default=DEFAULT_INCLUDE),
        exclude=_clean_patterns(value.get("exclude"), default=DEFAULT_EXCLUDE),
        rules=_clean_rule_dicts(raw_rules),
        path=path,
        migrated_from=(
            str(value["migrated_from"])
            if value.get("migrated_from")
            else None
        ),
        migration_required_review=bool(
            value.get("migration_required_review", False)
        ),
        migration_rejected_rule_count=max(
            0,
            int(value.get("migration_rejected_rule_count", 0)),
        ),
    )


def _profile_document(snapshot: ProfileSnapshot) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "profile_id": snapshot.profile_id,
        "profile_name": snapshot.profile_name,
        "target": snapshot.target_dir,
        "target_dir": snapshot.target_dir,
        "revision": snapshot.revision,
        "enabled": snapshot.enabled,
        "quiet_seconds": snapshot.quiet_seconds,
        "include": list(snapshot.include),
        "exclude": list(snapshot.exclude),
        "rules": [dict(item) for item in snapshot.rules],
        "migrated_from": snapshot.migrated_from,
        "migration_required_review": snapshot.migration_required_review,
        "migration_rejected_rule_count": snapshot.migration_rejected_rule_count,
        "updated_at": _utc_now(),
    }


def load_profile(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
    profile: str | None = None,
    legacy_rules: Iterable[Mapping[str, Any]] = (),
    legacy_path: str | Path | None = None,
    migrate: bool = True,
) -> ProfileSnapshot:
    target = _validated_target_directory(target_dir)
    path = profile_path(target, state_root=state_root, profile=profile)
    lock_path = path.with_suffix(".lock")
    for candidate in (path, lock_path):
        _validated_state_document_path(
            candidate,
            state_root=state_root,
            category="profiles",
            relative_parts=2,
            require_exists=False,
        )
    with _ExclusiveFileLock(lock_path):
        if path.exists():
            snapshot = _read_profile_document(path, state_root=state_root)
            if Path(snapshot.target_dir) != target:
                raise SorterV2Error(
                    f"Profile target mismatch: {snapshot.target_dir} != {target}"
                )
            return snapshot

        rules = _clean_rule_dicts(legacy_rules)
        migrated_from = str(Path(legacy_path).resolve()) if legacy_path else None
        snapshot = ProfileSnapshot(
            profile_id=profile_id_for(target, profile),
            profile_name=profile,
            target_dir=str(target),
            revision=0,
            enabled=False,
            quiet_seconds=DEFAULT_QUIET_SECONDS,
            include=DEFAULT_INCLUDE,
            exclude=DEFAULT_EXCLUDE,
            rules=rules,
            path=path,
            migrated_from=migrated_from if rules else None,
        )
        if migrate:
            _validated_state_document_path(
                path,
                state_root=state_root,
                category="profiles",
                relative_parts=2,
                require_exists=False,
            )
            _atomic_write_json(path, _profile_document(snapshot))
        return snapshot


def save_profile(
    snapshot: ProfileSnapshot,
    *,
    rules: Iterable[Mapping[str, Any]] | None = None,
    enabled: bool | None = None,
    quiet_seconds: float | None = None,
    include: Iterable[str] | None = None,
    exclude: Iterable[str] | None = None,
    expected_revision: int | None = None,
    acknowledge_migration_review: bool = False,
) -> ProfileSnapshot:
    path = snapshot.path
    if path.name != "profile.json" or len(path.parents) < 3:
        raise SorterV2Error(f"Invalid profile state path: {path}")
    state_root = path.parents[2]
    target = _validated_target_directory(
        snapshot.target_dir,
        label="Profile target",
    )
    expected_path = profile_path(
        target,
        state_root=state_root,
        profile=snapshot.profile_name,
    )
    if (
        snapshot.profile_id != expected_path.parent.name
        or not _same_path_identity(path, expected_path)
    ):
        raise SorterV2Error(
            f"Profile identity does not match its state path: {path}"
        )
    _validated_state_document_path(
        path,
        state_root=state_root,
        category="profiles",
        relative_parts=2,
        require_exists=False,
    )
    lock_path = path.with_suffix(".lock")
    _validated_state_document_path(
        lock_path,
        state_root=state_root,
        category="profiles",
        relative_parts=2,
        require_exists=False,
    )
    with _ExclusiveFileLock(lock_path):
        if path.exists():
            current = _read_profile_document(path, state_root=state_root)
            current_revision = current.revision
        else:
            current = snapshot
            current_revision = snapshot.revision
        expected = snapshot.revision if expected_revision is None else expected_revision
        if current_revision != expected:
            raise RuleConflictError(
                f"Profile revision changed: expected {expected}, found {current_revision}"
            )
        migration_required_review = (
            current.migration_required_review
            and not acknowledge_migration_review
        )
        next_enabled = current.enabled if enabled is None else bool(enabled)
        if next_enabled and migration_required_review:
            raise SorterV2Error(
                "Legacy rule migration must be reviewed before automatic "
                "classification can be enabled."
            )
        next_snapshot = ProfileSnapshot(
            profile_id=current.profile_id,
            profile_name=current.profile_name,
            target_dir=current.target_dir,
            revision=current_revision + 1,
            enabled=next_enabled,
            quiet_seconds=(
                current.quiet_seconds
                if quiet_seconds is None
                else max(0.0, float(quiet_seconds))
            ),
            include=(
                current.include
                if include is None
                else _clean_patterns(include, default=DEFAULT_INCLUDE)
            ),
            exclude=(
                current.exclude
                if exclude is None
                else _clean_patterns(exclude, default=DEFAULT_EXCLUDE)
            ),
            rules=(
                current.rules
                if rules is None
                else _clean_rule_dicts(rules)
            ),
            path=path,
            migrated_from=current.migrated_from,
            migration_required_review=migration_required_review,
            migration_rejected_rule_count=(
                0
                if acknowledge_migration_review
                else current.migration_rejected_rule_count
            ),
        )
        _atomic_write_json(path, _profile_document(next_snapshot))
        return next_snapshot


def list_profiles(
    *,
    state_root: str | Path | None = None,
) -> list[ProfileSnapshot]:
    try:
        profiles_dir = _state_category_root(state_root, "profiles")
    except SorterV2Error:
        return []
    if not profiles_dir.is_dir():
        return []
    snapshots: list[ProfileSnapshot] = []
    for path in profiles_dir.glob("*/profile.json"):
        try:
            snapshots.append(
                _read_profile_document(path, state_root=state_root)
            )
        except SorterV2Error:
            continue
    return sorted(
        snapshots,
        key=lambda item: (item.target_dir.casefold(), item.profile_id.casefold()),
    )


def is_partial_file(path: str | Path) -> bool:
    name = Path(path).name.casefold()
    return any(name.endswith(suffix) for suffix in PARTIAL_SUFFIXES)


def _best_effort_unlocked(path: Path) -> bool | None:
    """Return False for a detected lock and None when locking is unavailable."""

    if os.name == "nt":
        try:
            import msvcrt

            with path.open("rb") as stream:
                if path.stat().st_size == 0:
                    return True
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                except OSError as error:
                    if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        return False
                    return None
                finally:
                    try:
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    except OSError:
                        pass
            return True
        except PermissionError:
            return False
        except (ImportError, OSError):
            return None

    try:
        import fcntl

        with path.open("rb") as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                if error.errno in {errno.EACCES, errno.EAGAIN}:
                    return False
                return None
            finally:
                try:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        return True
    except (ImportError, OSError):
        return None


def check_file_stability(
    path: str | Path,
    *,
    quiet_seconds: float = DEFAULT_QUIET_SECONDS,
    now_ns: int | None = None,
    check_lock: bool = True,
) -> StabilityResult:
    source = Path(path)
    if is_partial_file(source):
        return StabilityResult(False, "partial-file-suffix", None)
    if _is_link_or_reparse(source):
        return StabilityResult(False, "link-or-reparse-point", None)
    try:
        stat = source.stat()
    except OSError as error:
        return StabilityResult(False, f"stat-failed:{error}", None)
    if not source.is_file():
        return StabilityResult(False, "not-a-regular-file", None)
    fingerprint = FileFingerprint(size=stat.st_size, mtime_ns=stat.st_mtime_ns)
    age_ns = (time.time_ns() if now_ns is None else now_ns) - stat.st_mtime_ns
    if age_ns < max(0.0, quiet_seconds) * 1_000_000_000:
        return StabilityResult(False, "quiet-period", fingerprint)
    if check_lock:
        unlocked = _best_effort_unlocked(source)
        if unlocked is False:
            return StabilityResult(False, "file-locked", fingerprint)
    return StabilityResult(True, None, fingerprint)


def same_volume(source: str | Path, destination_dir: str | Path) -> bool:
    try:
        return os.stat(source).st_dev == os.stat(destination_dir).st_dev
    except OSError:
        return False


def new_plan(
    target_dir: str | Path,
    *,
    profile_id: str,
    rules_revision: int,
    quiet_seconds: float,
    operations: Iterable[PlanOperation],
    skipped: Iterable[SkippedFile],
) -> OrganizePlan:
    target = _validated_target_directory(target_dir)
    return OrganizePlan(
        plan_id=str(uuid.uuid4()),
        target_dir=str(target),
        profile_id=profile_id,
        rules_revision=rules_revision,
        quiet_seconds=max(0.0, float(quiet_seconds)),
        operations=list(operations),
        skipped=list(skipped),
    )


def _validated_id(value: str, label: str) -> str:
    cleaned = str(value).strip()
    if not _SAFE_ID_RE.fullmatch(cleaned):
        raise SorterV2Error(f"Invalid {label}: {value}")
    return cleaned


def save_plan(
    plan: OrganizePlan,
    *,
    state_root: str | Path | None = None,
) -> Path:
    root = resolve_state_root(state_root)
    target = _validated_target_directory(
        plan.target_dir,
        label="Plan target",
    )
    if not _same_path_identity(Path(plan.target_dir), target):
        raise SorterV2Error("Plan target must be canonical before it is saved.")
    for operation in plan.operations:
        _validate_operation_paths(target, operation)
    path = root / "plans" / f"{_validated_id(plan.plan_id, 'plan id')}.json"
    _validated_state_document_path(
        path,
        state_root=state_root,
        category="plans",
        relative_parts=1,
        require_exists=False,
    )
    if path.exists():
        raise SorterV2Error(f"Plan already exists: {plan.plan_id}")
    _atomic_write_json(path, plan.to_dict())
    return path


def load_plan(
    plan_id: str,
    *,
    state_root: str | Path | None = None,
) -> OrganizePlan:
    path = (
        resolve_state_root(state_root)
        / "plans"
        / f"{_validated_id(plan_id, 'plan id')}.json"
    )
    path = _validated_state_document_path(
        path,
        state_root=state_root,
        category="plans",
        relative_parts=1,
        require_exists=True,
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SorterV2Error(f"Cannot load plan {plan_id}: {error}") from error
    if not isinstance(value, dict):
        raise SorterV2Error(f"Invalid plan document: {path}")
    plan = OrganizePlan.from_dict(value)
    if plan.plan_id != path.stem:
        raise SorterV2Error(f"Plan identity does not match its state path: {path}")
    raw_target = Path(plan.target_dir).expanduser()
    if not raw_target.is_absolute():
        raise SorterV2Error(f"Plan target is not absolute: {path}")
    canonical_target = raw_target.resolve(strict=False)
    if not _same_path_identity(raw_target, canonical_target):
        raise SorterV2Error(f"Plan target is not canonical: {path}")
    return plan


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class _Journal:
    def __init__(
        self,
        plan: OrganizePlan,
        *,
        state_root: str | Path | None = None,
    ) -> None:
        self.transaction_id = str(uuid.uuid4())
        self.path = (
            resolve_state_root(state_root)
            / "journals"
            / f"{self.transaction_id}.json"
        )
        _validated_state_document_path(
            self.path,
            state_root=state_root,
            category="journals",
            relative_parts=1,
            require_exists=False,
        )
        self.value: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "transaction_id": self.transaction_id,
            "plan_id": plan.plan_id,
            "profile_id": plan.profile_id,
            "target_dir": plan.target_dir,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "status": "planned",
            "operations": [
                {
                    **item.to_dict(),
                    "status": "planned",
                    "staging": None,
                    "sha256": None,
                    "error": None,
                }
                for item in plan.operations
            ],
            "errors": [],
        }
        self.flush()

    def flush(self) -> None:
        self.value["updated_at"] = _utc_now()
        _atomic_write_json(self.path, self.value)

    def set_transaction_status(self, status: str) -> None:
        self.value["status"] = status
        self.flush()

    def update_operation(self, index: int, **updates: Any) -> None:
        self.value["operations"][index].update(updates)
        self.flush()

    def add_error(self, message: str) -> None:
        self.value["errors"].append(message)
        self.flush()


def _fingerprint(path: Path) -> FileFingerprint:
    stat = path.stat()
    return FileFingerprint(stat.st_size, stat.st_mtime_ns)


def _expected_fingerprint(operation: PlanOperation) -> FileFingerprint:
    return FileFingerprint(operation.source_size, operation.source_mtime_ns)


def _ensure_source_unchanged(source: Path, operation: PlanOperation) -> None:
    if _is_link_or_reparse(source):
        raise SorterV2Error(
            f"Source was replaced by a link or reparse point: {source}"
        )
    try:
        actual = _fingerprint(source)
    except OSError as error:
        raise SorterV2Error(f"Source unavailable: {source}: {error}") from error
    if actual != _expected_fingerprint(operation):
        raise SorterV2Error(f"Source changed after preview: {source}")


def _fsync_file(path: Path) -> None:
    # Windows rejects fsync on a descriptor opened read-only.  A linked source
    # may itself be read-only, so durability is best effort in that case.
    try:
        with path.open("r+b") as stream:
            os.fsync(stream.fileno())
    except OSError:
        pass


def _windows_alternate_streams(
    path: Path,
) -> tuple[tuple[str, int, str], ...] | None:
    """Return named NTFS streams, or ``None`` when streams are unsupported."""

    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _WIN32_FIND_STREAM_DATA(ctypes.Structure):
        _fields_ = [
            ("StreamSize", ctypes.c_longlong),
            ("cStreamName", wintypes.WCHAR * (260 + 36)),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(_WIN32_FIND_STREAM_DATA),
        wintypes.DWORD,
    ]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(_WIN32_FIND_STREAM_DATA),
    ]
    find_next.restype = wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [wintypes.HANDLE]
    find_close.restype = wintypes.BOOL

    data = _WIN32_FIND_STREAM_DATA()
    handle = find_first(str(path), 0, ctypes.byref(data), 0)
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        error = ctypes.get_last_error()
        if error in {1, 2, 38, 50, 87}:
            return None
        raise ctypes.WinError(error)

    streams: list[tuple[str, int, str]] = []
    try:
        while True:
            name = str(data.cStreamName)
            if name and name.casefold() != "::$data":
                stream_path = f"{path}{name}"
                streams.append(
                    (name, int(data.StreamSize), sha256_file(stream_path))
                )
            if find_next(handle, ctypes.byref(data)):
                continue
            error = ctypes.get_last_error()
            if error != 38:
                raise ctypes.WinError(error)
            break
    finally:
        find_close(handle)
    return tuple(sorted(streams, key=lambda item: item[0].casefold()))


def _posix_extended_attributes(
    path: Path,
) -> tuple[tuple[str, str], ...] | None:
    """Return content hashes for supported POSIX extended attributes."""

    list_xattr = getattr(os, "listxattr", None)
    get_xattr = getattr(os, "getxattr", None)
    if os.name == "nt" or not callable(list_xattr) or not callable(get_xattr):
        return None
    try:
        try:
            names = list_xattr(path, follow_symlinks=False)
        except TypeError:
            names = list_xattr(path)
        values: list[tuple[str, str]] = []
        for raw_name in names:
            try:
                raw_value = get_xattr(path, raw_name, follow_symlinks=False)
            except TypeError:
                raw_value = get_xattr(path, raw_name)
            values.append(
                (
                    str(raw_name),
                    hashlib.sha256(raw_value).hexdigest(),
                )
            )
        return tuple(sorted(values, key=lambda item: item[0]))
    except OSError as error:
        unsupported = {
            errno.ENOSYS,
            getattr(errno, "ENOTSUP", 95),
            getattr(errno, "EOPNOTSUPP", 95),
        }
        if error.errno in unsupported:
            return None
        raise


def _capture_file_metadata(path: Path) -> _FileMetadata:
    value = path.stat()
    attributes = getattr(value, "st_file_attributes", None)
    return _FileMetadata(
        mtime_ns=value.st_mtime_ns,
        permissions=stat_module.S_IMODE(value.st_mode),
        file_attributes=int(attributes) if attributes is not None else None,
        alternate_streams=_windows_alternate_streams(path),
        extended_attributes=_posix_extended_attributes(path),
    )


def _file_metadata_to_dict(value: _FileMetadata) -> dict[str, Any]:
    return {
        "mtime_ns": value.mtime_ns,
        "permissions": value.permissions,
        "file_attributes": value.file_attributes,
        "alternate_streams": (
            None
            if value.alternate_streams is None
            else [
                {"name": name, "size": size, "sha256": digest}
                for name, size, digest in value.alternate_streams
            ]
        ),
        "extended_attributes": (
            None
            if value.extended_attributes is None
            else [
                {"name": name, "sha256": digest}
                for name, digest in value.extended_attributes
            ]
        ),
    }


def _file_metadata_from_dict(value: Any) -> _FileMetadata | None:
    if not isinstance(value, Mapping):
        return None
    alternate_value = value.get("alternate_streams")
    alternate_streams: tuple[tuple[str, int, str], ...] | None
    if alternate_value is None:
        alternate_streams = None
    elif isinstance(alternate_value, list):
        parsed: list[tuple[str, int, str]] = []
        for item in alternate_value:
            if not isinstance(item, Mapping):
                return None
            parsed.append(
                (
                    str(item.get("name", "")),
                    int(item.get("size", -1)),
                    str(item.get("sha256", "")),
                )
            )
        alternate_streams = tuple(parsed)
    else:
        return None
    extended_value = value.get("extended_attributes")
    extended_attributes: tuple[tuple[str, str], ...] | None
    if extended_value is None:
        extended_attributes = None
    elif isinstance(extended_value, list):
        extended_parsed: list[tuple[str, str]] = []
        for item in extended_value:
            if not isinstance(item, Mapping):
                return None
            extended_parsed.append(
                (
                    str(item.get("name", "")),
                    str(item.get("sha256", "")),
                )
            )
        extended_attributes = tuple(extended_parsed)
    else:
        return None
    try:
        attributes = value.get("file_attributes")
        return _FileMetadata(
            mtime_ns=int(value["mtime_ns"]),
            permissions=int(value["permissions"]),
            file_attributes=(
                int(attributes) if attributes is not None else None
            ),
            alternate_streams=alternate_streams,
            extended_attributes=extended_attributes,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _verify_file_metadata(
    path: Path,
    expected: _FileMetadata,
    *,
    label: str,
) -> None:
    actual = _capture_file_metadata(path)
    mismatches: list[str] = []
    if actual.mtime_ns != expected.mtime_ns:
        mismatches.append("modification timestamp")
    if actual.permissions != expected.permissions:
        mismatches.append("permissions")
    if (
        expected.file_attributes is not None
        and actual.file_attributes != expected.file_attributes
    ):
        mismatches.append("Windows file attributes")
    if (
        expected.alternate_streams is not None
        and actual.alternate_streams != expected.alternate_streams
    ):
        mismatches.append("Windows alternate data streams")
    if (
        expected.extended_attributes is not None
        and actual.extended_attributes != expected.extended_attributes
    ):
        mismatches.append("extended attributes")
    if mismatches:
        raise SorterV2Error(
            f"{label} metadata verification failed ({', '.join(mismatches)}): {path}"
        )


def _copy_windows_exclusive(source: Path, destination: Path) -> None:
    import ctypes
    from ctypes import wintypes

    copy_file = ctypes.WinDLL("kernel32", use_last_error=True).CopyFileW
    copy_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.BOOL]
    copy_file.restype = wintypes.BOOL
    if copy_file(str(source), str(destination), True):
        return
    error = ctypes.get_last_error()
    if error in {80, 183}:
        raise FileExistsError(error, os.strerror(error), str(destination))
    raise ctypes.WinError(error)


def _set_windows_file_attributes(path: Path, attributes: int) -> None:
    import ctypes
    from ctypes import wintypes

    set_attributes = ctypes.WinDLL(
        "kernel32",
        use_last_error=True,
    ).SetFileAttributesW
    set_attributes.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    set_attributes.restype = wintypes.BOOL
    if not set_attributes(str(path), attributes):
        raise ctypes.WinError(ctypes.get_last_error())


def _unlink_file_preserving_failure(path: Path, *, missing_ok: bool = False) -> None:
    """Delete a file, temporarily clearing Windows read-only when necessary."""

    try:
        path.unlink(missing_ok=missing_ok)
        return
    except PermissionError:
        if os.name != "nt" or not path.exists():
            raise

    attributes = getattr(path.stat(), "st_file_attributes", None)
    readonly = 0x1
    if attributes is None or not (int(attributes) & readonly):
        raise PermissionError(f"Cannot delete file: {path}")
    original_attributes = int(attributes)
    _set_windows_file_attributes(path, original_attributes & ~readonly)
    try:
        path.unlink(missing_ok=missing_ok)
    except BaseException:
        if path.exists():
            _set_windows_file_attributes(path, original_attributes)
        raise


def _copy_file_exclusive_preserving_metadata(
    source: Path,
    destination: Path,
) -> None:
    if os.name == "nt":
        _copy_windows_exclusive(source, destination)
        _fsync_file(destination)
        return

    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            shutil.copyfileobj(reader, writer, length=1024 * 1024)
            writer.flush()
            os.fsync(writer.fileno())
        # copystat also copies extended attributes on platforms where Python
        # and the underlying filesystem support them.
        shutil.copystat(source, destination, follow_symlinks=False)
        _fsync_file(destination)
    except BaseException:
        # A failed exclusive copy may still contain diagnostic bytes. Keep the
        # transaction-owned partial instead of risking deletion after a path
        # replacement race; recovery will quarantine or review it.
        raise


def _copy_to_staging(
    source: Path,
    destination_dir: Path,
    transaction_id: str,
    operation_id: str,
) -> tuple[Path, str, int]:
    stage = destination_dir / (
        f".filesorter-{transaction_id[:8]}-{operation_id[:8]}.partial"
    )
    _copy_file_exclusive_preserving_metadata(source, stage)
    _fsync_directory(destination_dir)
    return stage, sha256_file(source), source.stat().st_size


def _copy_stage_exclusive(stage: Path, destination: Path) -> None:
    _copy_file_exclusive_preserving_metadata(stage, destination)


def _publish_staging(stage: Path, destination: Path) -> None:
    if os.name == "nt":
        # A Windows hard link shares the read-only attribute with the staging
        # name, which makes safe staging cleanup mutate the published file.
        # CopyFileW keeps them independent and still provides fail-if-exists.
        try:
            _copy_stage_exclusive(stage, destination)
        except FileExistsError as conflict:
            raise SorterV2Error(
                f"Destination appeared after preview: {destination}"
            ) from conflict
        _fsync_directory(destination.parent)
        return

    try:
        os.link(stage, destination)
        _fsync_file(destination)
    except FileExistsError:
        raise SorterV2Error(f"Destination appeared after preview: {destination}")
    except OSError as error:
        unsupported = {
            errno.EXDEV,
            errno.EPERM,
            errno.EACCES,
            getattr(errno, "EOPNOTSUPP", 95),
            getattr(errno, "ENOTSUP", 95),
        }
        if error.errno not in unsupported:
            raise
        try:
            _copy_stage_exclusive(stage, destination)
        except FileExistsError as conflict:
            raise SorterV2Error(
                f"Destination appeared after preview: {destination}"
            ) from conflict
    _fsync_directory(destination.parent)


def _staged_move(
    source: Path,
    destination: Path,
    operation: PlanOperation,
    journal: _Journal,
    index: int,
) -> str:
    _validate_operation_paths(source.parent, operation)
    source_metadata = _capture_file_metadata(source)
    stage_name = (
        destination.parent
        / f".filesorter-{journal.transaction_id[:8]}-{operation.operation_id[:8]}.partial"
    )
    journal.update_operation(index, status="copying", staging=str(stage_name))
    stage, source_hash, bytes_copied = _copy_to_staging(
        source,
        destination.parent,
        journal.transaction_id,
        operation.operation_id,
    )
    _ensure_source_unchanged(source, operation)
    stage_stat = stage.stat()
    stage_hash = sha256_file(stage)
    if (
        bytes_copied != operation.source_size
        or stage_stat.st_size != operation.source_size
        or source_hash != stage_hash
    ):
        raise SorterV2Error(f"Staging verification failed for {source}")
    _verify_file_metadata(stage, source_metadata, label="Staging")
    journal.update_operation(
        index,
        status="verified",
        staging=str(stage),
        sha256=source_hash,
        metadata=_file_metadata_to_dict(source_metadata),
    )
    _validate_operation_paths(source.parent, operation)
    if _is_link_or_reparse(stage) or not stage.is_file():
        raise SorterV2Error(f"Staging path is no longer a regular file: {stage}")
    journal.update_operation(
        index,
        status="publishing",
        publication_confirmed=False,
        publication_method="staged-copy",
    )
    _publish_staging(stage, destination)
    if destination.stat().st_size != operation.source_size:
        raise SorterV2Error(
            f"Published size verification failed; preserved for review: {destination}"
        )
    if sha256_file(destination) != source_hash:
        raise SorterV2Error(
            "Published SHA-256 verification failed; preserved for review: "
            f"{destination}"
        )
    _verify_file_metadata(
        destination,
        source_metadata,
        label="Published file",
    )
    _unlink_file_preserving_failure(stage, missing_ok=True)
    _fsync_directory(destination.parent)
    journal.update_operation(
        index,
        status="published",
        staging=None,
        publication_confirmed=True,
        publication_method="staged-copy",
    )
    _validate_operation_paths(source.parent, operation)
    _ensure_source_unchanged(source, operation)
    if sha256_file(source) != source_hash:
        raise SorterV2Error(f"Source changed before deletion: {source}")
    _verify_file_metadata(source, source_metadata, label="Source")
    if sha256_file(destination) != source_hash:
        raise SorterV2Error(
            f"Published file changed before source deletion: {destination}"
        )
    _verify_file_metadata(
        destination,
        source_metadata,
        label="Published file before source deletion",
    )
    _unlink_file_preserving_failure(source)
    _fsync_directory(source.parent)
    journal.update_operation(index, status="committed")
    return source_hash


def _same_volume_move(
    source: Path,
    destination: Path,
    operation: PlanOperation,
    journal: _Journal,
    index: int,
) -> str:
    _validate_operation_paths(source.parent, operation)
    source_hash = sha256_file(source)
    _ensure_source_unchanged(source, operation)
    journal.update_operation(
        index,
        status="publishing",
        sha256=source_hash,
        publication_confirmed=False,
        publication_method="hardlink",
    )
    try:
        os.link(source, destination)
    except FileExistsError as error:
        raise SorterV2Error(
            f"Destination appeared after preview: {destination}"
        ) from error
    except OSError as error:
        unsupported = {
            errno.EXDEV,
            errno.EPERM,
            errno.EACCES,
            getattr(errno, "EOPNOTSUPP", 95),
            getattr(errno, "ENOTSUP", 95),
        }
        if error.errno in unsupported:
            return _staged_move(source, destination, operation, journal, index)
        raise
    _fsync_file(destination)
    _fsync_directory(destination.parent)
    if not os.path.samefile(source, destination):
        raise SorterV2Error(
            f"Published hard link does not identify the source file: {destination}"
        )
    journal.update_operation(
        index,
        status="published",
        publication_confirmed=True,
        publication_method="hardlink",
    )
    _validate_operation_paths(source.parent, operation)
    _ensure_source_unchanged(source, operation)
    if sha256_file(destination) != source_hash:
        raise SorterV2Error(f"Linked file changed before deletion: {destination}")
    if not os.path.samefile(source, destination):
        raise SorterV2Error(
            f"Published hard link changed before source deletion: {destination}"
        )
    source.unlink()
    _fsync_directory(source.parent)
    journal.update_operation(index, status="committed")
    return source_hash


def _existing_plan_execution(
    plan: OrganizePlan,
    *,
    state_root: str | Path | None,
) -> dict[str, Any] | None:
    journals_dir = _state_category_root(state_root, "journals")
    if not journals_dir.is_dir():
        return None
    for path in journals_dir.glob("*.json"):
        try:
            value = _read_journal(path, state_root=state_root)
        except SorterV2Error:
            continue
        if not isinstance(value, dict) or value.get("plan_id") != plan.plan_id:
            continue
        status = str(value.get("status", ""))
        operations = value.get("operations", [])
        if status in {"committed", "completed_with_errors"}:
            errors = [str(item) for item in value.get("errors", [])]
            return {
                "ok": not errors,
                "type": "file-sorter-result",
                "schema_version": SCHEMA_VERSION,
                "plan_id": plan.plan_id,
                "transaction_id": str(value.get("transaction_id", "")),
                "journal_path": str(path),
                "target_dir": plan.target_dir,
                "moved_count": sum(
                    1
                    for item in operations
                    if item.get("status") == "committed"
                ),
                "unmatched_count": sum(
                    1 for item in plan.skipped if item.category == "unmatched"
                ),
                "skipped_count": len(plan.skipped),
                "errors": errors,
                "replayed": True,
            }
        raise SorterV2Error(
            f"Plan {plan.plan_id} already has transaction "
            f"{value.get('transaction_id')} in state {status}."
        )
    return None


def _target_lock_path(
    target_dir: str | Path,
    *,
    state_root: str | Path | None,
) -> Path:
    target = _validated_target_directory(target_dir)
    del state_root
    return target


def _validate_operation_paths(
    target: Path,
    operation: PlanOperation,
    *,
    allow_missing_source: bool = False,
) -> tuple[Path, Path]:
    target = _validated_target_directory(target)
    source = Path(operation.source)
    destination = Path(operation.destination)
    if not source.is_absolute() or source.parent != target:
        raise SorterV2Error(f"Source escaped plan target: {source}")
    if not destination.is_absolute():
        raise SorterV2Error(f"Destination is not absolute: {destination}")
    folder = Path(operation.folder.strip()).expanduser()
    if folder.is_absolute():
        raise SorterV2Error(
            f"Destination rule escaped plan target: {operation.folder}"
        )
    if (
        not operation.folder.strip()
        or operation.folder.strip() in {".", ".."}
        or folder.name != operation.folder.strip()
        or "/" in operation.folder
        or "\\" in operation.folder
    ):
        raise SorterV2Error(
            f"Invalid destination rule in plan: {operation.folder}"
        )
    expected_destination_dir = target / operation.folder.strip()
    if expected_destination_dir.parent != target:
        raise SorterV2Error(
            f"Destination escaped plan target: {expected_destination_dir}"
        )
    if (
        not expected_destination_dir.exists()
        or not expected_destination_dir.is_dir()
    ):
        raise SorterV2Error(
            f"Destination directory no longer exists: {expected_destination_dir}"
        )
    if _is_link_or_reparse(expected_destination_dir):
        raise SorterV2Error("Plan paths cannot use links or reparse points.")
    if not _same_path_identity(
        expected_destination_dir,
        expected_destination_dir.resolve(strict=True),
    ):
        raise SorterV2Error(
            f"Destination directory escaped plan target: {expected_destination_dir}"
        )
    if (
        os.path.ismount(expected_destination_dir)
        or expected_destination_dir.stat().st_dev != target.stat().st_dev
    ):
        raise SorterV2Error(
            "Destination directory cannot cross a mounted filesystem boundary."
        )
    if destination.parent != expected_destination_dir:
        raise SorterV2Error(
            f"Destination does not match the plan rule: {destination}"
        )
    source_exists_or_link = source.exists() or source.is_symlink()
    if not source_exists_or_link and not allow_missing_source:
        raise SorterV2Error(f"Source no longer exists: {source}")
    if source_exists_or_link:
        if _is_link_or_reparse(source):
            raise SorterV2Error("Plan paths cannot use links or reparse points.")
        canonical_source = source.resolve(strict=True)
        if (
            not _same_path_identity(source, canonical_source)
            or canonical_source.parent != target
            or not canonical_source.is_file()
            or canonical_source.stat().st_dev != target.stat().st_dev
        ):
            raise SorterV2Error(f"Source escaped plan target: {source}")
    if destination.exists() or destination.is_symlink():
        if _is_link_or_reparse(destination):
            raise SorterV2Error("Plan paths cannot use links or reparse points.")
        canonical_destination = destination.resolve(strict=True)
        if (
            not _same_path_identity(destination, canonical_destination)
            or canonical_destination.parent != expected_destination_dir
            or not canonical_destination.is_file()
        ):
            raise SorterV2Error(f"Destination escaped plan target: {destination}")
    return source, destination


def _validate_journal_operation_paths(
    journal: Mapping[str, Any],
    operation: Mapping[str, Any],
) -> tuple[Path, Path, Path | None]:
    target_text = str(journal.get("target_dir", "")).strip()
    if not target_text:
        raise SorterV2Error("Journal does not contain a target directory.")
    target = _validated_target_directory(
        target_text,
        label="Journal target directory",
    )

    source = Path(str(operation.get("source", ""))).expanduser()
    destination = Path(str(operation.get("destination", ""))).expanduser()
    if not source.is_absolute() or source.parent != target:
        raise SorterV2Error("Journal source escaped its target directory.")
    if not destination.is_absolute():
        raise SorterV2Error("Journal destination is not absolute.")

    folder = str(operation.get("folder", "")).strip()
    if not folder:
        raise SorterV2Error(
            "Journal operation lacks a bounded destination rule."
        )
    probe = PlanOperation(
        operation_id=str(operation.get("operation_id") or "legacy-operation"),
        source=str(source),
        destination=str(destination),
        keyword=str(operation.get("keyword", "")),
        folder=folder,
        rule_source=str(operation.get("rule_source", "legacy")),
        source_size=int(operation.get("source_size") or 0),
        source_mtime_ns=int(operation.get("source_mtime_ns") or 0),
        transfer=str(operation.get("transfer", "unknown")),
    )
    source, destination = _validate_operation_paths(
        target,
        probe,
        allow_missing_source=True,
    )

    stage_value = operation.get("staging")
    if not stage_value:
        return source, destination, None
    stage = Path(str(stage_value)).expanduser()
    operation_id = str(operation.get("operation_id", "")).strip()
    transaction_id = str(journal.get("transaction_id", "")).strip()
    expected_stage_name = (
        f".filesorter-{transaction_id[:8]}-{operation_id[:8]}.partial"
    )
    if (
        not stage.is_absolute()
        or not operation_id
        or not transaction_id
        or stage.name != expected_stage_name
        or stage.parent != destination.parent
    ):
        raise SorterV2Error("Journal staging path is not owned by this operation.")
    if stage.exists() or stage.is_symlink():
        if _is_link_or_reparse(stage):
            raise SorterV2Error("Journal staging path cannot be a link.")
        if not _same_path_identity(stage, stage.resolve(strict=True)):
            raise SorterV2Error("Journal staging path escaped its directory.")
    return source, destination, stage


def _execute_plan_under_lock(
    plan: OrganizePlan,
    *,
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    target = _validated_target_directory(
        plan.target_dir,
        label="Plan target",
    )
    if _plan_expired(plan):
        raise SorterV2Error(
            f"Plan {plan.plan_id} expired; create a new preview."
        )
    existing = _existing_plan_execution(plan, state_root=state_root)
    if existing is not None:
        return existing
    journal = _Journal(plan, state_root=state_root)
    journal.set_transaction_status("in_progress")
    moved_count = 0
    errors: list[str] = []
    for index, operation in enumerate(plan.operations):
        source = Path(operation.source)
        destination = Path(operation.destination)
        try:
            source, destination = _validate_operation_paths(target, operation)
            if not destination.parent.is_dir():
                raise SorterV2Error(
                    f"Destination directory no longer exists: {destination.parent}"
                )
            if destination.exists():
                raise SorterV2Error(
                    f"Destination appeared after preview: {destination}"
                )
            _ensure_source_unchanged(source, operation)
            stability = check_file_stability(
                source,
                quiet_seconds=plan.quiet_seconds,
            )
            if not stability.stable:
                raise SorterV2Error(
                    f"Source is not stable ({stability.reason}): {source}"
                )
            journal.update_operation(index, status="running")
            if same_volume(source, destination.parent):
                digest = _same_volume_move(
                    source,
                    destination,
                    operation,
                    journal,
                    index,
                )
            else:
                digest = _staged_move(
                    source,
                    destination,
                    operation,
                    journal,
                    index,
                )
            journal.update_operation(index, sha256=digest)
            moved_count += 1
        except (OSError, SorterV2Error, TypeError, ValueError) as error:
            message = f"{source.name}: {error}"
            errors.append(message)
            try:
                journal_operation = journal.value["operations"][index]
                stage_value = journal_operation.get("staging")
                if stage_value and journal_operation.get("status") in {
                    "copying",
                    "verified",
                    "publishing",
                    "published",
                }:
                    _unlink_file_preserving_failure(
                        Path(stage_value),
                        missing_ok=True,
                    )
                updates: dict[str, Any] = {"error": str(error)}
                if not journal_operation.get("publication_confirmed"):
                    updates["status"] = "failed"
                journal.update_operation(index, **updates)
                journal.add_error(message)
            except OSError:
                pass
    journal.set_transaction_status(
        "committed" if not errors else "completed_with_errors"
    )
    return {
        "ok": not errors,
        "type": "file-sorter-result",
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan.plan_id,
        "transaction_id": journal.transaction_id,
        "journal_path": str(journal.path),
        "target_dir": plan.target_dir,
        "moved_count": moved_count,
        "unmatched_count": sum(
            1 for item in plan.skipped if item.category == "unmatched"
        ),
        "skipped_count": len(plan.skipped),
        "errors": errors,
    }


def execute_plan(
    plan: OrganizePlan,
    *,
    state_root: str | Path | None = None,
    lock_timeout_seconds: float = 5.0,
    pre_execute_validate: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Execute a plan while holding the target-wide repository lock."""

    with _TargetDirectoryLock(
        plan.target_dir,
        timeout_seconds=max(0.0, lock_timeout_seconds),
    ):
        if pre_execute_validate is not None:
            pre_execute_validate()
        return _execute_plan_under_lock(plan, state_root=state_root)


def _read_journal(
    path: Path,
    *,
    state_root: str | Path | None,
) -> dict[str, Any]:
    path = _validated_state_document_path(
        path,
        state_root=state_root,
        category="journals",
        relative_parts=1,
        require_exists=True,
    )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SorterV2Error(f"Cannot read journal {path}: {error}") from error
    if not isinstance(value, dict):
        raise SorterV2Error(f"Invalid journal document: {path}")
    if str(value.get("transaction_id", "")).strip() != path.stem:
        raise SorterV2Error(
            f"Journal identity does not match its state path: {path}"
        )
    target_text = str(value.get("target_dir", "")).strip()
    target = Path(target_text).expanduser()
    if not target_text or not target.is_absolute():
        raise SorterV2Error(f"Journal target is missing or invalid: {path}")
    if not _same_path_identity(target, target.resolve(strict=False)):
        raise SorterV2Error(f"Journal target is not canonical: {path}")
    return value


def transaction_history(
    *,
    state_root: str | Path | None = None,
    target_dir: str | Path | None = None,
) -> list[dict[str, Any]]:
    journals_dir = _state_category_root(state_root, "journals")
    if not journals_dir.is_dir():
        return []
    target = (
        str(_validated_target_directory(target_dir))
        if target_dir is not None
        else None
    )
    history: list[dict[str, Any]] = []
    for path in journals_dir.glob("*.json"):
        try:
            value = _read_journal(path, state_root=state_root)
        except SorterV2Error:
            continue
        if target is not None and value.get("target_dir") != target:
            continue
        operations = value.get("operations", [])
        history.append(
            {
                "transaction_id": value.get("transaction_id"),
                "plan_id": value.get("plan_id"),
                "profile_id": value.get("profile_id"),
                "target_dir": value.get("target_dir"),
                "status": value.get("status"),
                "created_at": value.get("created_at"),
                "updated_at": value.get("updated_at"),
                "moved_count": sum(
                    1
                    for item in operations
                    if item.get("status") in {"committed", "undone"}
                ),
                "error_count": len(value.get("errors", [])),
            }
        )
    return sorted(
        history,
        key=lambda item: str(item.get("created_at", "")),
        reverse=True,
    )


def _journal_path(
    transaction_id: str,
    state_root: str | Path | None,
) -> Path:
    path = (
        resolve_state_root(state_root)
        / "journals"
        / f"{_validated_id(transaction_id, 'transaction id')}.json"
    )
    return _validated_state_document_path(
        path,
        state_root=state_root,
        category="journals",
        relative_parts=1,
        require_exists=False,
    )


def _validate_undo_move_paths(
    moved_path: Path,
    original_path: Path,
) -> tuple[Path, Path]:
    target = _validated_target_directory(
        original_path.parent,
        label="Undo target",
    )
    moved_parent = moved_path.parent
    if (
        moved_parent.parent != target
        or not moved_parent.is_dir()
        or _is_link_or_reparse(moved_parent)
        or not _same_path_identity(
            moved_parent,
            moved_parent.resolve(strict=True),
        )
        or os.path.ismount(moved_parent)
        or moved_parent.stat().st_dev != target.stat().st_dev
    ):
        raise SorterV2Error(
            f"Undo moved path escaped the target folder: {moved_path}"
        )
    for candidate, label in (
        (moved_path, "Undo moved file"),
        (original_path, "Undo original file"),
    ):
        if candidate.exists() or candidate.is_symlink():
            if _is_link_or_reparse(candidate):
                raise SorterV2Error(f"{label} cannot be a link: {candidate}")
            resolved = candidate.resolve(strict=True)
            if (
                not _same_path_identity(candidate, resolved)
                or not resolved.is_file()
            ):
                raise SorterV2Error(f"{label} escaped its folder: {candidate}")
    return moved_path, original_path


def _move_exact_for_undo(
    source: Path,
    destination: Path,
    *,
    expected_hash: str,
    on_published: Callable[[str, _FileMetadata], None],
) -> None:
    _validate_undo_move_paths(source, destination)
    if destination.exists() or destination.is_symlink():
        raise SorterV2Error(f"Undo source path is occupied: {destination}")
    if source.is_symlink() or not source.is_file():
        raise SorterV2Error(f"Moved file is missing: {source}")
    if sha256_file(source) != expected_hash:
        raise SorterV2Error(f"Moved file changed since transaction: {source}")
    source_metadata = _capture_file_metadata(source)
    if same_volume(source, destination.parent):
        try:
            os.link(source, destination)
        except FileExistsError as error:
            raise SorterV2Error(f"Undo source path is occupied: {destination}") from error
        except OSError as error:
            if error.errno not in {
                errno.EXDEV,
                errno.EPERM,
                errno.EACCES,
                getattr(errno, "EOPNOTSUPP", 95),
                getattr(errno, "ENOTSUP", 95),
            }:
                raise
        else:
            _fsync_file(destination)
            _validate_undo_move_paths(source, destination)
            if not os.path.samefile(source, destination):
                raise SorterV2Error("Undo hard-link ownership proof failed.")
            _verify_file_metadata(
                destination,
                source_metadata,
                label="Undo hard-link publication",
            )
            on_published("hardlink", source_metadata)
            _validate_undo_move_paths(source, destination)
            if not os.path.samefile(source, destination):
                raise SorterV2Error(
                    "Undo hard-link ownership changed after publication proof."
                )
            if sha256_file(source) != expected_hash:
                raise SorterV2Error(
                    f"Moved file changed after undo publication: {source}"
                )
            _verify_file_metadata(source, source_metadata, label="Undo source")
            source.unlink()
            _fsync_directory(source.parent)
            _fsync_directory(destination.parent)
            return

    stage = destination.parent / f".filesorter-undo-{uuid.uuid4().hex}.partial"
    stage_created = False
    try:
        _copy_file_exclusive_preserving_metadata(source, stage)
        stage_created = True
        if sha256_file(stage) != expected_hash:
            raise SorterV2Error(f"Undo staging verification failed: {source}")
        _verify_file_metadata(stage, source_metadata, label="Undo staging")
        _validate_undo_move_paths(source, destination)
        _publish_staging(stage, destination)
        if sha256_file(destination) != expected_hash:
            _unlink_file_preserving_failure(destination, missing_ok=True)
            raise SorterV2Error(f"Undo publish verification failed: {destination}")
        try:
            _verify_file_metadata(
                destination,
                source_metadata,
                label="Undo published file",
            )
        except SorterV2Error:
            _unlink_file_preserving_failure(destination, missing_ok=True)
            raise
        _verify_file_metadata(source, source_metadata, label="Undo source")
        _unlink_file_preserving_failure(stage, missing_ok=True)
        _fsync_directory(destination.parent)
        on_published("staged-copy", source_metadata)
        _validate_undo_move_paths(source, destination)
        if sha256_file(source) != expected_hash:
            raise SorterV2Error(
                f"Moved file changed after undo publication: {source}"
            )
        if sha256_file(destination) != expected_hash:
            raise SorterV2Error(
                f"Restored file changed after undo publication: {destination}"
            )
        _verify_file_metadata(source, source_metadata, label="Undo source")
        _verify_file_metadata(
            destination,
            source_metadata,
            label="Undo published file",
        )
        _unlink_file_preserving_failure(source)
        _fsync_directory(source.parent)
        _fsync_directory(destination.parent)
    finally:
        if stage_created:
            _unlink_file_preserving_failure(stage, missing_ok=True)


def _resume_undo_move(
    moved_path: Path,
    original_path: Path,
    *,
    expected_hash: str,
    operation: Mapping[str, Any],
    on_published: Callable[[str, _FileMetadata], None],
) -> None:
    _validate_undo_move_paths(moved_path, original_path)

    moved_exists = moved_path.exists()
    original_exists = original_path.exists()
    if moved_exists:
        if not moved_path.is_file() or sha256_file(moved_path) != expected_hash:
            raise SorterV2Error(
                f"Moved file changed since transaction: {moved_path}"
            )
    if original_exists:
        if not original_path.is_file() or sha256_file(original_path) != expected_hash:
            raise SorterV2Error(
                f"Restored file does not match the transaction: {original_path}"
            )

    if moved_exists and not original_exists:
        _move_exact_for_undo(
            moved_path,
            original_path,
            expected_hash=expected_hash,
            on_published=on_published,
        )
        return
    if original_exists and not moved_exists:
        return
    if moved_exists and original_exists:
        _validate_undo_move_paths(moved_path, original_path)
        publication_method = str(
            operation.get("undo_publication_method") or ""
        )
        publication_metadata = _file_metadata_from_dict(
            operation.get("undo_publication_metadata")
        )
        proof_matches = (
            operation.get("undo_publication_confirmed") is True
            and operation.get("undo_publication_sha256") == expected_hash
            and operation.get("undo_publication_source") == str(moved_path)
            and operation.get("undo_publication_destination") == str(original_path)
            and publication_method in {"hardlink", "staged-copy"}
            and publication_metadata is not None
        )
        if not proof_matches or publication_metadata is None:
            raise SorterV2Error(
                "Both undo paths exist without a durable publication proof; "
                "both copies were preserved for manual review."
            )
        _verify_file_metadata(
            moved_path,
            publication_metadata,
            label="Undo retained source",
        )
        _verify_file_metadata(
            original_path,
            publication_metadata,
            label="Undo published destination",
        )
        if publication_method == "hardlink" and not os.path.samefile(
            moved_path,
            original_path,
        ):
            raise SorterV2Error(
                "Undo hard-link publication proof no longer identifies one file; "
                "both copies were preserved."
            )
        _unlink_file_preserving_failure(moved_path)
        _fsync_directory(moved_path.parent)
        _fsync_directory(original_path.parent)
        return
    raise SorterV2Error(
        f"Neither the moved nor restored file exists: {moved_path}"
    )


def _undo_journal(
    path: Path,
    value: dict[str, Any],
) -> dict[str, Any]:
    transaction_id = str(value.get("transaction_id", path.stem))
    errors: list[str] = []
    undone_count = 0
    operations = value.get("operations", [])
    if not isinstance(operations, list):
        operations = []
        value["operations"] = operations
    has_pending_undo = any(
        isinstance(operation, dict)
        and operation.get("status") in {"committed", "undoing", "undo_failed"}
        for operation in operations
    )
    if has_pending_undo:
        value["status"] = "undo_in_progress"
        value["updated_at"] = _utc_now()
        _atomic_write_json(path, value)
    for operation in reversed(operations):
        if not isinstance(operation, dict):
            continue
        if operation.get("status") not in {
            "committed",
            "undoing",
            "undo_failed",
        }:
            continue
        moved_path = Path(str(operation.get("destination", "")))
        original_path = Path(str(operation.get("source", "")))
        expected_hash = str(operation.get("sha256") or "")
        try:
            original_path, moved_path, _stage = _validate_journal_operation_paths(
                value,
                operation,
            )
            if not expected_hash:
                raise SorterV2Error("Journal does not contain a SHA-256 digest.")
            previous_status = str(operation.get("status") or "")
            if previous_status == "committed":
                operation.update(
                    {
                        "undo_publication_confirmed": False,
                        "undo_publication_method": None,
                        "undo_publication_sha256": None,
                        "undo_publication_source": None,
                        "undo_publication_destination": None,
                        "undo_publication_metadata": None,
                    }
                )
            operation["status"] = "undoing"
            value["updated_at"] = _utc_now()
            _atomic_write_json(path, value)

            def persist_undo_publication(
                method: str,
                metadata: _FileMetadata,
            ) -> None:
                operation.update(
                    {
                        "undo_publication_confirmed": True,
                        "undo_publication_method": method,
                        "undo_publication_sha256": expected_hash,
                        "undo_publication_source": str(moved_path),
                        "undo_publication_destination": str(original_path),
                        "undo_publication_metadata": _file_metadata_to_dict(metadata),
                    }
                )
                value["updated_at"] = _utc_now()
                _atomic_write_json(path, value)

            _resume_undo_move(
                moved_path,
                original_path,
                expected_hash=expected_hash,
                operation=operation,
                on_published=persist_undo_publication,
            )
            operation["status"] = "undone"
            operation["undone_at"] = _utc_now()
            undone_count += 1
        except (OSError, SorterV2Error, TypeError, ValueError) as error:
            operation["status"] = "undo_failed"
            operation["undo_error"] = str(error)
            errors.append(f"{moved_path.name}: {error}")
        value["updated_at"] = _utc_now()
        _atomic_write_json(path, value)
    value["status"] = "undone" if not errors else "undo_failed"
    value["updated_at"] = _utc_now()
    if errors:
        value.setdefault("errors", []).extend(errors)
    _atomic_write_json(path, value)
    return {
        "ok": not errors,
        "type": "file-sorter-undo-result",
        "transaction_id": transaction_id,
        "undone_count": undone_count,
        "errors": errors,
    }


def _undo_transaction_under_lock(
    transaction_id: str,
    *,
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    path = _journal_path(transaction_id, state_root)
    return _undo_journal(
        path,
        _read_journal(path, state_root=state_root),
    )


def _journal_has_interrupted_undo(value: Mapping[str, Any]) -> bool:
    if value.get("status") == "undo_in_progress":
        return True
    operations = value.get("operations", [])
    return isinstance(operations, list) and any(
        isinstance(operation, dict)
        and operation.get("status") == "undoing"
        for operation in operations
    )


def undo_transaction(
    transaction_id: str,
    *,
    state_root: str | Path | None = None,
    lock_timeout_seconds: float = 5.0,
) -> dict[str, Any]:
    path = _journal_path(transaction_id, state_root)
    value = _read_journal(path, state_root=state_root)
    target_dir = str(value.get("target_dir", "")).strip()
    if not target_dir:
        raise SorterV2Error(f"Journal has no target: {path}")
    with _TargetDirectoryLock(
        target_dir,
        timeout_seconds=max(0.0, lock_timeout_seconds),
    ):
        return _undo_transaction_under_lock(
            transaction_id,
            state_root=state_root,
        )


def undo_last_transaction(
    target_dir: str | Path,
    *,
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    for item in transaction_history(state_root=state_root, target_dir=target_dir):
        if item.get("status") in {"committed", "completed_with_errors"}:
            return undo_transaction(
                str(item["transaction_id"]),
                state_root=state_root,
            )
    raise SorterV2Error(f"No committed transaction found for {target_dir}")


def recover_transactions(
    *,
    state_root: str | Path | None = None,
    target_dir: str | Path | None = None,
    lock_timeout_seconds: float = 5.0,
) -> list[dict[str, Any]]:
    """Recover interrupted publications and remove owned staging files.

    A source is deleted only when the journal durably confirms publication and
    both source and destination still match the recorded SHA-256 digest.
    """

    target = (
        str(_validated_target_directory(target_dir))
        if target_dir is not None
        else None
    )
    results: list[dict[str, Any]] = []
    journals_dir = _state_category_root(state_root, "journals")
    if not journals_dir.is_dir():
        return results
    for path in journals_dir.glob("*.json"):
        try:
            value = _read_journal(path, state_root=state_root)
        except SorterV2Error:
            continue
        if target is not None and value.get("target_dir") != target:
            continue
        if (
            value.get("status") in TERMINAL_TRANSACTION_STATES
            and not _journal_has_interrupted_undo(value)
        ):
            continue
        journal_target = str(value.get("target_dir", "")).strip()
        if not journal_target or not Path(journal_target).is_absolute():
            results.append(
                {
                    "transaction_id": value.get("transaction_id"),
                    "status": "recovery_failed",
                    "recovered_count": 0,
                    "errors": ["Journal target is missing or invalid."],
                }
            )
            continue
        try:
            with _TargetDirectoryLock(
                journal_target,
                timeout_seconds=max(0.0, lock_timeout_seconds),
            ):
                current = _read_journal(path, state_root=state_root)
                if (
                    current.get("status") in TERMINAL_TRANSACTION_STATES
                    and not _journal_has_interrupted_undo(current)
                ):
                    continue
                results.append(_recover_journal(path, current))
        except SorterV2Error as error:
            results.append(
                {
                    "transaction_id": value.get("transaction_id"),
                    "status": "busy",
                    "recovered_count": 0,
                    "errors": [str(error)],
                }
            )
    return results


def _recover_journal(
    path: Path,
    value: dict[str, Any],
) -> dict[str, Any]:
    if _journal_has_interrupted_undo(value):
        undo_result = _undo_journal(path, value)
        return {
            "transaction_id": undo_result["transaction_id"],
            "status": "undone" if undo_result["ok"] else "undo_failed",
            "recovered_count": undo_result["undone_count"],
            "errors": undo_result["errors"],
            "recovery_kind": "undo",
        }

    recovered = 0
    errors: list[str] = []
    operations = value.get("operations", [])
    if not isinstance(operations, list):
        operations = []
        value["operations"] = operations
    for operation in operations:
        if not isinstance(operation, dict):
            continue
        source = Path(str(operation.get("source", "")))
        destination = Path(str(operation.get("destination", "")))
        status = str(operation.get("status", "planned"))
        digest = str(operation.get("sha256") or "")
        publication_proven = (
            status == "published"
            or operation.get("publication_confirmed") is True
        )
        try:
            source, destination, stage = _validate_journal_operation_paths(
                value,
                operation,
            )
            if stage is not None and stage.exists():
                _unlink_file_preserving_failure(stage)
            if publication_proven and digest:
                if (
                    destination.is_file()
                    and source.is_file()
                ):
                    if (
                        sha256_file(destination) == digest
                        and sha256_file(source) == digest
                    ):
                        expected_metadata = _file_metadata_from_dict(
                            operation.get("metadata")
                        )
                        if expected_metadata is not None:
                            _verify_file_metadata(
                                source,
                                expected_metadata,
                                label="Recovery source",
                            )
                            _verify_file_metadata(
                                destination,
                                expected_metadata,
                                label="Recovery destination",
                            )
                        source, destination, _ = (
                            _validate_journal_operation_paths(
                                value,
                                operation,
                            )
                        )
                        if (
                            operation.get("publication_method") == "hardlink"
                            and not os.path.samefile(source, destination)
                        ):
                            raise SorterV2Error(
                                "Recovery hard-link ownership proof failed."
                            )
                        _unlink_file_preserving_failure(source)
                        operation["status"] = "committed"
                        operation["staging"] = None
                        recovered += 1
                        continue
                elif (
                    destination.is_file()
                    and not source.exists()
                ):
                    if sha256_file(destination) == digest:
                        operation["status"] = "committed"
                        operation["staging"] = None
                        recovered += 1
                        continue
            if status not in {"committed", "undone"}:
                operation["status"] = "recovery_failed"
                retained_error = (
                    "Source retained; operation was not safely published."
                )
                operation["error"] = retained_error
                errors.append(f"{source.name}: {retained_error}")
        except (OSError, SorterV2Error, TypeError, ValueError) as error:
            operation["status"] = "recovery_failed"
            operation["error"] = str(error)
            errors.append(f"{source.name}: {error}")
    unfinished = [
        item
        for item in operations
        if isinstance(item, dict)
        and item.get("status") not in {"committed", "undone"}
    ]
    value["status"] = "committed" if not unfinished else "completed_with_errors"
    value["updated_at"] = _utc_now()
    if errors:
        value.setdefault("errors", []).extend(errors)
    _atomic_write_json(path, value)
    return {
        "transaction_id": value.get("transaction_id"),
        "status": value["status"],
        "recovered_count": recovered,
        "errors": errors,
    }
