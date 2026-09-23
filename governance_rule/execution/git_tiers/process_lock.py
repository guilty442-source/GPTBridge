"""Crash-safe process lock shared by Git automation services."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


class LockBusyError(RuntimeError):
    """Raised when a live process owns the requested lock."""


_NATIVE_PROBE = None
_NATIVE_PROBE_TRIED = False


def native_process_api():
    """Lazily load the governed native binding for process probes.

    The Git tier dependency direction is frozen (A529/A495): git_tiers
    must not import shared-layer Python modules.  ``_sovereign_native``
    is the same C execution-tier backend that ``process_metrics``
    dispatches to — loading it directly keeps the tier self-contained.
    """
    global _NATIVE_PROBE, _NATIVE_PROBE_TRIED
    if _NATIVE_PROBE_TRIED:
        return _NATIVE_PROBE
    _NATIVE_PROBE_TRIED = True
    try:
        import importlib
        import sys

        root = Path(__file__).resolve().parents[3]
        for candidate in (
            root / "main-system" / "dist-native",
            root / "main-system" / "src-core" / "core_system" / "native",
        ):
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            try:
                _NATIVE_PROBE = importlib.import_module("_sovereign_native")
                return _NATIVE_PROBE
            except ImportError:
                continue
    except Exception:
        pass
    return _NATIVE_PROBE


def _pid_alive_os(pid: int) -> bool:
    """Fallback liveness probe without the native binding (fail-safe)."""
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))
        if handle:
            exit_code = ctypes.c_ulong()
            kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
            kernel32.CloseHandle(handle)
            return exit_code.value == 259  # STILL_ACTIVE
        # ERROR_INVALID_PARAMETER (87): the pid does not exist.  Any other
        # failure (e.g. access denied) means a live protected process —
        # never treat an unknown owner as dead or the lock could be stolen.
        return kernel32.GetLastError() != 87
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def pid_alive(pid: int) -> bool:
    """Return whether ``pid`` refers to a live process (native first)."""
    native = native_process_api()
    if native is not None:
        try:
            return bool(native.process_alive(int(pid)))
        except (OSError, ValueError, RuntimeError):
            return False
    return _pid_alive_os(pid)


def _owner_alive(path: Path) -> bool:
    try:
        age = max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return False
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
        pid = int(payload["pid"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return age < 10.0
    return pid_alive(pid)


def lock_is_active(path: Path) -> bool:
    """Return true for a live or freshly-created lock."""
    return path.exists() and _owner_alive(path)


class ProcessFileLock:
    """Atomic lock that only removes the caller's own token."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.token = uuid.uuid4().hex
        self.fd: int | None = None

    def __enter__(self) -> "ProcessFileLock":
        for attempt in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                payload = json.dumps({"pid": os.getpid(), "token": self.token})
                os.write(self.fd, payload.encode("ascii"))
                os.fsync(self.fd)
                return self
            except FileExistsError as exc:
                if _owner_alive(self.path) or attempt:
                    raise LockBusyError(f"lock-busy:{self.path.name}") from exc
                self.path.unlink(missing_ok=True)
        raise LockBusyError(f"lock-busy:{self.path.name}")

    def __exit__(self, *_: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            payload = json.loads(self.path.read_text(encoding="ascii"))
            if payload.get("token") == self.token:
                self.path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass
