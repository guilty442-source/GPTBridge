"""Process/system metrics facade — P24 psutil convergence (A219/A221).

Single Python entry point for process/system queries.  Native primitives
(``gptbridge_native_*`` via ``_sovereign_native``) are the sole backend —
P24 retired psutil from requirements, so the transition fallback is gone.
When the native backend cannot answer, the functions return the
documented fail-closed value (``-1``/``None``/``False``) rather than
raising, matching the existing callers' graceful-degradation contracts.

Ownership (A221/E186): Python owns memory; native calls are read-only
queries that never cross-allocate.  No unbounded work — enumeration is
capped, cpu_percent uses the same two-sample delta contract as psutil.
"""
from __future__ import annotations

import os
import time
from typing import Any, Iterable, Optional

from .native_dispatcher import _load_native


def _native() -> Any:
    return _load_native()


def native_metrics_available() -> bool:
    return _native() is not None


def metrics_available() -> bool:
    """Whether the native metrics backend can answer (fail-closed otherwise)."""
    return _native() is not None


# ---------------------------------------------------------------------------
# system memory
# ---------------------------------------------------------------------------

def system_memory_total_bytes() -> int:
    n = _native()
    if n is not None:
        v = int(n.system_memory_total_bytes())
        if v > 0:
            return v
    return -1


def system_memory_available_bytes() -> int:
    n = _native()
    if n is not None:
        v = int(n.system_memory_available_bytes())
        if v > 0:
            return v
    return -1


def virtual_memory_percent() -> Optional[float]:
    total = system_memory_total_bytes()
    avail = system_memory_available_bytes()
    if total > 0 and avail >= 0:
        return round(100.0 * (total - avail) / total, 1)
    return None


def cpu_count() -> int:
    n = _native()
    if n is not None:
        v = int(n.cpu_count())
        if v > 0:
            return v
    return os.cpu_count() or 0


# ---------------------------------------------------------------------------
# cpu percent (two-sample delta, same contract as psutil cpu_percent)
# ---------------------------------------------------------------------------

_sys_cpu_prev: Optional[tuple[float, float, float]] = None
_proc_cpu_prev: dict[int, tuple[float, float]] = {}


def _system_cpu_sample() -> Optional[tuple[float, float, float]]:
    """(timestamp, busy_100ns, total_100ns) or None."""
    n = _native()
    if n is not None:
        t = n.system_cpu_times()
        if t:
            idle, kernel, user = (float(x) for x in t)
            # kernel includes idle (GetSystemTimes contract)
            busy = kernel - idle + user
            total = kernel + user
            if total > 0:
                return (time.monotonic(), busy, total)
    return None


def cpu_percent(interval: Optional[float] = None) -> float:
    """System-wide CPU utilisation percent.

    ``interval=None`` → percent since the previous call (psutil contract);
    a numeric interval samples twice with that sleep between.  Returns
    ``-1.0`` when no backend can sample.
    """
    global _sys_cpu_prev
    first = _system_cpu_sample()
    if first is None:
        return -1.0
    if interval is not None and interval > 0:
        time.sleep(interval)
        second = _system_cpu_sample()
        prev = first
        if second is None:
            return -1.0
    else:
        second = first
        prev = _sys_cpu_prev
    _sys_cpu_prev = second
    if prev is None:
        return 0.0  # first call has no baseline — psutil returns 0.0
    db, dt = second[1] - prev[1], second[2] - prev[2]
    return round(max(0.0, min(100.0, 100.0 * db / dt)), 1) if dt > 0 else 0.0


def process_cpu_percent(pid: int, interval: Optional[float] = None) -> float:
    """Per-process CPU percent (same two-sample contract as psutil).

    ``sample()`` normalises to **seconds of CPU time** on both backends
    (native returns 100ns ticks, psutil returns seconds), so the cached
    previous sample stays unit-consistent even if the backend flips.
    """
    def sample() -> Optional[float]:
        n = _native()
        if n is not None:
            t = n.process_cpu_times(int(pid))
            if t:
                return float(t[0] + t[1]) / 1e7
            return None
        return None

    first = sample()
    if first is None:
        return -1.0
    wall0 = time.monotonic()
    if interval is not None and interval > 0:
        time.sleep(interval)
        second = sample()
        if second is None:
            return -1.0
        wall1 = time.monotonic()
        prev = first
    else:
        second = first
        wall1 = wall0
        prev_tuple = _proc_cpu_prev.get(int(pid))
        prev = prev_tuple[0] if prev_tuple else None
        if prev_tuple:
            wall0 = prev_tuple[1]
    _proc_cpu_prev[int(pid)] = (second, wall1)
    if prev is None:
        return 0.0
    wall_dt = wall1 - wall0
    if wall_dt <= 0:
        return 0.0
    busy_s = second - prev
    ncpu = max(1, cpu_count())
    return round(max(0.0, min(100.0 * ncpu, 100.0 * busy_s / wall_dt)), 1)


# ---------------------------------------------------------------------------
# process queries
# ---------------------------------------------------------------------------

def process_alive(pid: int) -> bool:
    n = _native()
    if n is not None:
        return bool(n.process_alive(int(pid)))
    return False


def process_name(pid: int) -> Optional[str]:
    n = _native()
    if n is not None:
        v = n.process_name(int(pid))
        if v:
            return str(v)
    return None


def process_working_set_bytes(pid: int) -> int:
    n = _native()
    if n is not None:
        v = int(n.process_working_set_bytes(int(pid)))
        if v >= 0:
            return v
    return -1


def process_private_bytes(pid: int) -> int:
    n = _native()
    if n is not None:
        v = n.process_private_bytes(int(pid))
        if v >= 0:
            return v
    return -1



def process_list(max_count: int = 65536) -> list[int]:
    n = _native()
    if n is not None:
        v = n.process_list(int(max_count))
        if v is not None:
            return [int(x) for x in v]
    return []


def process_children(pid: int, max_count: int = 4096) -> list[int]:
    n = _native()
    if n is not None:
        v = n.process_children(int(pid), int(max_count))
        if v is not None:
            return [int(x) for x in v]
        return []
    return []


def process_iter_names(max_count: int = 65536) -> list[tuple[int, str]]:
    """(pid, name) pairs — the process_iter(["pid","name"]) replacement."""
    return [
        (pid, process_name(pid) or "")
        for pid in process_list(max_count)
    ]


def process_terminate(pid: int) -> bool:
    n = _native()
    if n is not None:
        return bool(n.process_terminate(int(pid)))
    return False


def process_exe(pid: int) -> Optional[str]:
    n = _native()
    if n is not None:
        v = n.process_exe(int(pid))
        if v:
            return str(v)
    return None


def process_cmdline(pid: int) -> Optional[str]:
    """Full command line as a single string (argv joined with spaces)."""
    n = _native()
    if n is not None:
        v = n.process_cmdline(int(pid))
        if v:
            return str(v)
        return None
    return None


def process_num_threads(pid: int) -> int:
    """Thread count — -1 when unavailable."""
    n = _native()
    if n is not None and hasattr(n, "process_num_threads"):
        v = int(n.process_num_threads(int(pid)))
        if v >= 0:
            return v
        return -1
    return -1


def process_num_handles(pid: int) -> int:
    """Open handle count (Windows) — -1 when unavailable."""
    n = _native()
    if n is not None and hasattr(n, "process_num_handles"):
        v = int(n.process_num_handles(int(pid)))
        if v >= 0:
            return v
        return -1
    return -1


def tcp_listen_pid(port: int) -> int:
    n = _native()
    if n is not None:
        return int(n.tcp_listen_pid(int(port)))
    return -1


# ---------------------------------------------------------------------------
# governor-facing primitives (P24: resource-governor migration)
# ---------------------------------------------------------------------------

# Windows priority classes — identical values to psutil.*_PRIORITY_CLASS.
PRIORITY_IDLE = 0x00000040
PRIORITY_BELOW_NORMAL = 0x00004000
PRIORITY_NORMAL = 0x00000020
PRIORITY_ABOVE_NORMAL = 0x00008000
PRIORITY_HIGH = 0x00000080


def process_parent(pid: int) -> int:
    n = _native()
    if n is not None and hasattr(n, "process_parent"):
        return int(n.process_parent(int(pid)))
    return -1


def process_parents(pid: int) -> list[int]:
    """Ancestor pid chain (nearest first); cycle-safe, bounded."""
    out: list[int] = []
    seen = {int(pid)}
    cur = int(pid)
    for _ in range(64):
        par = process_parent(cur)
        if par <= 0 or par in seen:
            break
        out.append(par)
        seen.add(par)
        cur = par
    return out


def process_create_time_ms(pid: int) -> int:
    """Creation time as Unix-epoch ms (pid-reuse identity anchor), or -1."""
    n = _native()
    if n is not None and hasattr(n, "process_create_time_ms"):
        return int(n.process_create_time_ms(int(pid)))
    return -1


def process_io_counters(pid: int) -> Optional[tuple[int, int]]:
    """(read_bytes, write_bytes) or None."""
    n = _native()
    if n is not None and hasattr(n, "process_io_counters"):
        v = n.process_io_counters(int(pid))
        if v is not None:
            return (int(v[0]), int(v[1]))
        return None
    return None


def process_username(pid: int) -> Optional[str]:
    """Owning account (DOMAIN\\user) of pid, or None."""
    n = _native()
    if n is not None and hasattr(n, "process_username"):
        v = n.process_username(int(pid))
        return str(v) if v else None
    return None


def process_set_priority(pid: int, win_class: int) -> bool:
    n = _native()
    if n is not None and hasattr(n, "process_set_priority"):
        return bool(n.process_set_priority(int(pid), int(win_class)))
    return False


def process_get_priority(pid: int) -> int:
    n = _native()
    if n is not None and hasattr(n, "process_get_priority"):
        return int(n.process_get_priority(int(pid)))
    return -1


def _mask_to_cores(mask: int) -> list[int]:
    return [i for i in range(64) if mask & (1 << i)]


def _cores_to_mask(cores: Iterable[int]) -> int:
    mask = 0
    for c in cores:
        c = int(c)
        if 0 <= c < 64:
            mask |= 1 << c
    return mask


def process_set_affinity(pid: int, cores: Iterable[int]) -> bool:
    mask = _cores_to_mask(cores)
    if mask == 0:
        return False
    n = _native()
    if n is not None and hasattr(n, "process_set_affinity"):
        return bool(n.process_set_affinity(int(pid), mask))
    return False


def process_get_affinity(pid: int) -> Optional[list[int]]:
    n = _native()
    if n is not None and hasattr(n, "process_get_affinity"):
        v = int(n.process_get_affinity(int(pid)))
        if v >= 0:
            return _mask_to_cores(v)
        return None
    return None


def process_wait(pid: int, timeout_s: float) -> bool:
    """True when pid exited within timeout_s."""
    n = _native()
    if n is not None and hasattr(n, "process_wait"):
        return bool(n.process_wait(int(pid), int(timeout_s * 1000)))
    return False


def process_info(pid: int) -> Optional[dict[str, Any]]:
    """Aggregated per-process snapshot — the ``proc.info`` replacement.

    Keys: pid, name, exe, username, create_time_ms, ppid, rss_bytes,
    io_read_bytes, io_write_bytes, num_threads, num_handles.  Returns
    ``None`` when the process is gone (fail-closed; callers treat as the
    old ``psutil.NoSuchProcess`` boundary).
    """
    pid = int(pid)
    if not process_alive(pid):
        return None
    io = process_io_counters(pid)
    return {
        "pid": pid,
        "name": process_name(pid),
        "exe": process_exe(pid),
        "username": process_username(pid),
        "create_time_ms": process_create_time_ms(pid),
        "ppid": process_parent(pid),
        "rss_bytes": process_working_set_bytes(pid),
        "io_read_bytes": io[0] if io else None,
        "io_write_bytes": io[1] if io else None,
        "num_threads": process_num_threads(pid),
        "num_handles": process_num_handles(pid),
    }
