"""Crash-safe process lock shared by Git automation services."""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path


class LockBusyError(RuntimeError):
    """Raised when a live process owns the requested lock."""


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
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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
