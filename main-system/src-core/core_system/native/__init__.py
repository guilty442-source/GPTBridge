"""Python adapter for the System Sovereign native kernel.

Loads the compiled ``_sovereign_native.pyd`` extension (if present) and exposes
a stable, typed surface to the RuntimeSubSovereign and MaintenanceSovereign.  If
the extension is not built, every function degrades gracefully so the platform
keeps running (Python equivalents are used where trivial).
"""

from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import pathlib
import sys
import time
from typing import Any

_NATIVE_FILENAME = "_sovereign_native"


def _load_native_extension() -> Any:
    """Load the governed native extension, or return None when not built.

    The artifact is built into ``main-system/dist-native`` and installed next
    to this package; a packaged layout may keep only the latter.
    """

    try:
        from . import _sovereign_native as native  # type: ignore

        return native
    except ImportError:  # pragma: no cover - depends on local build
        pass

    directory = pathlib.Path(__file__).resolve().parent
    candidates = [directory, directory.parents[2] / "dist-native"]
    for candidate in candidates:
        for suffix in importlib.machinery.EXTENSION_SUFFIXES:
            artifact = candidate / f"{_NATIVE_FILENAME}{suffix}"
            if not artifact.is_file():
                continue
            qualified = f"{__name__}.{_NATIVE_FILENAME}"
            try:
                spec = importlib.util.spec_from_file_location(qualified, artifact)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[qualified] = module
                spec.loader.exec_module(module)
            except Exception:
                sys.modules.pop(qualified, None)
                continue
            return module
    return None


_NATIVE = _load_native_extension()
_NATIVE_AVAILABLE = _NATIVE is not None

if _NATIVE_AVAILABLE:
    is_windows = _NATIVE.is_windows
    monotonic_seconds = _NATIVE.monotonic_seconds
    working_set_bytes = _NATIVE.working_set_bytes
    private_bytes = _NATIVE.private_bytes
    release_working_set = _NATIVE.release_working_set
else:

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
    """Whether the governed native compute extension is loaded."""

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
