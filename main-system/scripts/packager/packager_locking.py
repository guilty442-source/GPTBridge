from __future__ import annotations

import json
import os
import re
import stat as stat_module
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from packager_base import PackageOperationBusy
from packager_inventory import (
    _is_link_or_reparse,
    _persist_package_document,
    _process_is_alive,
)


@contextmanager
def package_operation_lock(
    tool_id: str,
    tool_dir: Path,
) -> Iterator[None]:
    build_root = tool_dir / "build"
    build_root.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(build_root) or build_root.resolve(strict=True) != build_root:
        raise RuntimeError(f"Package build root is unsafe: {build_root}")
    safe_tool_id = re.sub(r"[^0-9A-Za-z_.-]+", "_", tool_id) or "tool"
    lock_path = build_root / f".package-{safe_tool_id}.lock"
    token = uuid.uuid4().hex
    for attempt in range(2):
        try:
            lock_path.mkdir(mode=0o700)
            owner_path = lock_path / "owner.json"
            _persist_package_document(
                owner_path,
                {
                    "format_version": 1,
                    "tool_id": tool_id,
                    "pid": os.getpid(),
                    "token": token,
                    "created_at_utc": datetime.now(timezone.utc).isoformat(),
                },
            )
            break
        except FileExistsError as error:
            if attempt:
                raise PackageOperationBusy(
                    f"Could not acquire package lock for {tool_id}"
                ) from error
            lock_stat = lock_path.lstat()
            if (
                _is_link_or_reparse(lock_path)
                or not stat_module.S_ISDIR(lock_stat.st_mode)
                or lock_path.resolve(strict=True) != lock_path
            ):
                raise RuntimeError(f"Package lock is unsafe: {lock_path}")
            owner_path = lock_path / "owner.json"
            age_seconds = max(0.0, time.time() - lock_stat.st_mtime)
            owner: dict[str, Any] = {}
            if owner_path.exists() and not owner_path.is_symlink():
                try:
                    owner = json.loads(owner_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    owner = {}
            owner_valid = (
                owner.get("tool_id") == tool_id
                and isinstance(owner.get("pid"), int)
                and re.fullmatch(r"[0-9a-f]{32}", str(owner.get("token") or ""))
                is not None
            )
            if owner_valid and _process_is_alive(int(owner["pid"])):
                raise PackageOperationBusy(
                    f"Another package operation is active for {tool_id}"
                )
            if not owner_valid and age_seconds < 30:
                raise PackageOperationBusy(
                    f"Another package operation is initializing for {tool_id}"
                )
            stale_path = build_root / (
                f"package-lock-recovery-{safe_tool_id}-{uuid.uuid4().hex}"
            )
            os.replace(lock_path, stale_path)
    else:
        raise PackageOperationBusy(
            f"Could not acquire package lock for {tool_id}"
        )
    try:
        yield
    finally:
        try:
            owner_path = lock_path / "owner.json"
            owner = json.loads(owner_path.read_text(encoding="utf-8"))
            if (
                owner.get("tool_id") != tool_id
                or owner.get("pid") != os.getpid()
                or owner.get("token") != token
            ):
                raise RuntimeError("Package lock ownership changed")
            owner_path.unlink()
            lock_path.rmdir()
        except Exception:
            pass
