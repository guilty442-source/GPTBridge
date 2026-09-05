"""Python adapter for the System Sovereign native kernel.

Loads the compiled ``_sovereign_native.pyd`` extension (if present) and exposes
a stable, typed surface to the RuntimeSubSovereign and MaintenanceSovereign.  If
the extension is not built, every function degrades gracefully so the platform
keeps running (Python equivalents are used where trivial).
"""

from __future__ import annotations

import contextlib
import time
from typing import Any

try:
    from ._sovereign_native import (  # type: ignore
        is_windows,
        monotonic_seconds,
        private_bytes,
        release_working_set,
        working_set_bytes,
    )

    _NATIVE_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on local build
    _NATIVE_AVAILABLE = False

    def is_windows() -> bool:
        import os

        return os.name == "nt"

    def monotonic_seconds() -> float:
        return time.monotonic()

    def working_set_bytes() -> int:
        return -1

    def private_bytes() -> int:
        return -1

    def release_working_set() -> bool:
        import gc

        gc.collect()
        return False


def native_available() -> bool:
    """Whether the compiled C++ kernel is loaded (vs the Python fallback)."""

    return _NATIVE_AVAILABLE


def resource_status() -> dict[str, Any]:
    """Best-effort process resource snapshot (native where available)."""

    return {
        "native": native_available(),
        "windows": is_windows(),
        "working_set_bytes": int(working_set_bytes()),
        "private_bytes": int(private_bytes()),
        "monotonic_seconds": float(monotonic_seconds()),
    }


def release_resources() -> dict[str, Any]:
    """Trim process memory; returns a short evidence dict (never raises)."""

    result: dict[str, Any] = {}
    with contextlib.suppress(Exception):
        result["working_set_trimmed"] = bool(release_working_set())
    with contextlib.suppress(Exception):
        result["completed_at"] = monotonic_seconds()
    return result


__all__ = [
    "is_windows",
    "monotonic_seconds",
    "native_available",
    "private_bytes",
    "release_resources",
    "release_working_set",
    "resource_status",
    "working_set_bytes",
]
