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
import logging
import pathlib
import sys
import time
from typing import Any

_logger = logging.getLogger(__name__)

_NATIVE_FILENAME = "_sovereign_native"


def _load_native_extension() -> Any:
    """Load the governed native extension, or return None when not built.

    The artifact is built into ``main-system/dist-native`` and installed next
    to this package; a packaged layout may keep only the latter.  Prefer the
    build output because Windows can keep an installed ``.pyd`` locked while a
    governed rebuild has already produced the next artifact.

    The load is idempotent: once an artifact has been mapped into this
    process it is returned as-is.  Re-executing ``PyInit`` on a second file
    (or re-initing the same one after a mid-run rebuild swap) maps a second
    copy of the extension — observed to precede 0xc0000374 heap corruption
    (WER 2026-09-24).  A loaded native DLL can never be unloaded, so the
    first successful artifact stays authoritative for the process lifetime.
    """

    qualified = f"{__name__}.{_NATIVE_FILENAME}"
    existing = sys.modules.get(qualified)
    if existing is not None:
        return existing

    directory = pathlib.Path(__file__).resolve().parent
    candidates = [directory.parents[2] / "dist-native", directory]
    for candidate in candidates:
        for suffix in importlib.machinery.EXTENSION_SUFFIXES:
            artifact = candidate / f"{_NATIVE_FILENAME}{suffix}"
            if not artifact.is_file():
                continue
            try:
                spec = importlib.util.spec_from_file_location(qualified, artifact)
                if spec is None or spec.loader is None:
                    continue
                module = importlib.util.module_from_spec(spec)
                sys.modules[qualified] = module
                spec.loader.exec_module(module)
            except Exception as error:
                # The failed artifact stays mapped for the process lifetime;
                # record it so double-load incidents are diagnosable.
                _logger.warning(
                    "native_extension_load_failed artifact=%s error=%s",
                    artifact, error,
                )
                sys.modules.pop(qualified, None)
                continue
            _logger.info("native_extension_loaded artifact=%s", artifact)
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
    system_memory_total_bytes = _NATIVE.system_memory_total_bytes
    system_memory_available_bytes = _NATIVE.system_memory_available_bytes
    cpu_count = _NATIVE.cpu_count
    process_alive = _NATIVE.process_alive
    process_name = _NATIVE.process_name
    process_working_set_bytes = _NATIVE.process_working_set_bytes
    process_private_bytes = _NATIVE.process_private_bytes
    process_cpu_times = _NATIVE.process_cpu_times
    process_list = _NATIVE.process_list
    process_children = _NATIVE.process_children
    process_exe = _NATIVE.process_exe
    process_cmdline = _NATIVE.process_cmdline
    process_terminate = _NATIVE.process_terminate
    tcp_listen_pid = _NATIVE.tcp_listen_pid
    system_cpu_times = _NATIVE.system_cpu_times
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

    def system_memory_total_bytes() -> int:
        return -1

    def system_memory_available_bytes() -> int:
        return -1

    def cpu_count() -> int:
        import os

        return os.cpu_count() or 0

    def process_alive(pid: int) -> int:
        import os

        if pid <= 0:
            return 0
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            return 1
        except OSError:
            return 0
        return 1

    def process_name(pid: int) -> str | None:
        return None

    def process_working_set_bytes(pid: int) -> int:
        return -1

    def process_private_bytes(pid: int) -> int:
        return -1

    def process_cpu_times(pid: int) -> tuple[int, int] | None:
        return None

    def process_list(max_count: int) -> list[int] | None:
        return None

    def process_children(root_pid: int, max_count: int) -> list[int] | None:
        return None

    def process_exe(pid: int) -> str | None:
        return None

    def process_cmdline(pid: int) -> str | None:
        return None

    def process_terminate(pid: int) -> int:
        import os

        if pid <= 0:
            return 0
        try:
            os.kill(pid, 9)
        except OSError:
            return 0
        return 1

    def tcp_listen_pid(port: int) -> int:
        return -1

    def system_cpu_times() -> tuple[int, int, int] | None:
        return None


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


def virtual_memory_percent() -> float:
    """System memory pressure in percent, or -1.0 when unknown."""

    total = system_memory_total_bytes()
    avail = system_memory_available_bytes()
    if total <= 0 or avail < 0:
        return -1.0
    return float(total - avail) * 100.0 / float(total)


def system_cpu_percent(prev: tuple[int, int, int] | None) -> tuple[float, tuple[int, int, int] | None]:
    """System CPU busy fraction since ``prev`` sample.

    Returns ``(percent, new_sample)``; percent is -1.0 when the baseline or
    the platform primitive is unavailable.  Mirrors
    ``psutil.cpu_percent(interval=None)`` two-sample semantics.
    """

    sample = system_cpu_times()
    if sample is None:
        return -1.0, None
    if prev is None:
        return -1.0, sample
    idle_delta = sample[0] - prev[0]
    total_delta = (sample[1] - prev[1]) + (sample[2] - prev[2])
    if total_delta <= 0:
        return -1.0, sample
    busy = float(total_delta - idle_delta) / float(total_delta)
    return max(0.0, min(100.0, busy * 100.0)), sample


def process_cpu_percent(
    pid: int, prev: tuple[int, int, int, int] | None
) -> tuple[float, tuple[int, int, int, int] | None]:
    """Per-process CPU percent since ``prev`` = (kernel,user,wall_100ns,cores).

    Returns ``(percent, new_sample)``; percent is -1.0 on first sample or
    failure.  Matches ``psutil.Process.cpu_percent(interval=None)``.
    """

    times = process_cpu_times(pid)
    wall = int(monotonic_seconds() * 10_000_000)
    cores = cpu_count() or 1
    if times is None:
        return -1.0, None
    sample = (times[0], times[1], wall, cores)
    if prev is None:
        return -1.0, sample
    proc_delta = (times[0] - prev[0]) + (times[1] - prev[1])
    wall_delta = wall - prev[2]
    if wall_delta <= 0:
        return -1.0, sample
    pct = float(proc_delta) * 100.0 / float(wall_delta)
    return max(0.0, pct), sample


__all__ = [
    "cpu_count",
    "is_windows",
    "monotonic_seconds",
    "native_available",
    "private_bytes",
    "process_alive",
    "process_children",
    "process_cmdline",
    "process_cpu_percent",
    "process_cpu_times",
    "process_exe",
    "process_list",
    "process_name",
    "process_private_bytes",
    "process_terminate",
    "process_working_set_bytes",
    "release_resources",
    "release_working_set",
    "resource_status",
    "system_cpu_percent",
    "system_cpu_times",
    "system_memory_available_bytes",
    "system_memory_total_bytes",
    "tcp_listen_pid",
    "virtual_memory_percent",
    "working_set_bytes",
]
