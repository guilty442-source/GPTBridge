from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .analytics_helpers import _normalized_profile, utc_text
from .watch_repository import _validated_storage_path

DATA_ROOT_ENV = "GPTBRIDGE_AI_ASSISTANT_DATA_ROOT"
PROFILE_ENV = "GPTBRIDGE_AI_ASSISTANT_PROFILE"

def _pid_is_alive(pid: int) -> bool:
    """Compatibility diagnostic; ownership decisions use OS file locks."""

    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if process:
            try:
                exit_code = ctypes.c_ulong()
                if ctypes.windll.kernel32.GetExitCodeProcess(
                    process,
                    ctypes.byref(exit_code),
                ):
                    return exit_code.value == 259  # STILL_ACTIVE
                return True
            finally:
                ctypes.windll.kernel32.CloseHandle(process)
        return ctypes.get_last_error() == 5
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _try_lock_descriptor(descriptor: int) -> bool:
    """Acquire an OS-owned exclusive lock without trusting a PID file."""

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


class _RuntimeOwnerLock:
    def __init__(self, path: Path, component: str) -> None:
        self.path = path
        self.component = component
        self.token = uuid.uuid4().hex
        self.acquired = False
        self._descriptor: int | None = None

    def acquire(self) -> None:
        if self.acquired:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _validated_storage_path(
            self.path.parent,
            label=f"{self.component} ownership lock directory",
            require_exists=True,
            expected_kind="directory",
        )
        _validated_storage_path(
            self.path,
            label=f"{self.component} ownership lock",
            boundary=self.path.parent,
            expected_kind="file",
        )
        descriptor = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
        descriptor_locked = False
        try:
            descriptor_locked = _try_lock_descriptor(descriptor)
            if not descriptor_locked:
                owner_pid = ""
                try:
                    owner = json.loads(self.path.read_text(encoding="utf-8"))
                    parsed_pid = int(owner.get("pid") or 0)
                    owner_pid = f" (pid={parsed_pid})" if parsed_pid > 0 else ""
                except (OSError, ValueError, json.JSONDecodeError):
                    pass
                raise RuntimeError(
                    f"Another process is already active for {self.component}"
                    f"{owner_pid}"
                )
            document = {
                "pid": os.getpid(),
                "token": self.token,
                "component": self.component,
                "acquired_at": utc_text(),
            }
            payload = (json.dumps(document, ensure_ascii=False) + "\n").encode("utf-8")
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, payload)
            os.ftruncate(descriptor, len(payload))
            os.fsync(descriptor)
        except Exception:
            try:
                if descriptor_locked:
                    _unlock_descriptor(descriptor)
            except OSError:
                pass
            finally:
                os.close(descriptor)
            raise
        _fsync_directory(self.path.parent)
        self._descriptor = descriptor
        self.acquired = True

    def release(self) -> None:
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


def _runtime_root(tool_root: Path) -> Path:
    configured_root = str(os.environ.get(DATA_ROOT_ENV) or "").strip()
    profile = _normalized_profile(os.environ.get(PROFILE_ENV))
    if configured_root:
        base = _validated_storage_path(
            Path(configured_root),
            label="Configured AI investment data root",
            expected_kind="directory",
        )
        return _validated_storage_path(
            base / profile,
            label="AI investment analytics profile root",
            boundary=base,
            expected_kind="directory",
        )
    local_app_data = str(os.environ.get("LOCALAPPDATA") or "").strip()
    if local_app_data and tool_root.name.lower() == "ai-assistant":
        base = _validated_storage_path(
            Path(local_app_data),
            label="Local application data root",
            expected_kind="directory",
        )
        return _validated_storage_path(
            base / "GPTBridge" / "ai-assistant" / profile,
            label="AI investment analytics profile root",
            boundary=base,
            expected_kind="directory",
        )
    return _validated_storage_path(
        tool_root / "runtime",
        label="AI investment analytics runtime root",
        boundary=tool_root,
        expected_kind="directory",
    )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    _validated_storage_path(
        path.parent,
        label="Investment analytics data directory",
        expected_kind="directory",
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    _validated_storage_path(
        path.parent,
        label="Investment analytics data directory",
        require_exists=True,
        expected_kind="directory",
    )
    _validated_storage_path(
        path,
        label="Investment analytics data file",
        boundary=path.parent,
        expected_kind="file",
    )
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _copy_verified(source: Path, destination: Path) -> bool:
    payload = source.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    if destination.exists():
        return hashlib.sha256(destination.read_bytes()).hexdigest() == digest
    _atomic_write_bytes(destination, payload)
    if hashlib.sha256(destination.read_bytes()).hexdigest() != digest:
        raise OSError(f"runtime migration verification failed: {destination}")
    return True


def _decoded_json(value: Any, default: Any) -> Any:
    try:
        return json.loads(str(value or ""))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default



__all__ = ['DATA_ROOT_ENV', 'PROFILE_ENV', '_pid_is_alive', '_try_lock_descriptor', '_unlock_descriptor', '_RuntimeOwnerLock', '_runtime_root', '_fsync_directory', '_atomic_write_bytes', '_copy_verified', '_decoded_json']
