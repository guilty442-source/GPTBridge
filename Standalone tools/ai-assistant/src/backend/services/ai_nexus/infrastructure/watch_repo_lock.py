from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from .watch_repo_helpers import (
    _fsync_directory,
    _try_lock_descriptor,
    _unlock_descriptor,
    _validated_storage_path,
    utc_now,
)


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
            payload = {
                "pid": os.getpid(),
                "token": self.token,
                "component": self.component,
                "acquired_at": utc_now(),
            }
            encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, encoded)
            os.ftruncate(descriptor, len(encoded))
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
