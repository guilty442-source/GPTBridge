"""Cross-process file and directory locking primitives."""

from __future__ import annotations

import hashlib
import os
import time
import uuid
from pathlib import Path
from typing import Any

from ._constants import (
    _TARGET_LOCKS_GUARD,
    _TARGET_LOCKS_HELD,
    SorterV2Error,
)
from ._io_utils import _fsync_directory
from ._paths import _validated_target_directory


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

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            release_mutex = kernel32.ReleaseMutex
            release_mutex.argtypes = [wintypes.HANDLE]
            release_mutex.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL
            handle = self._mutex_handle
            self._mutex_handle = None
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
