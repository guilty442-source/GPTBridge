"""Process/system metrics facade — P24 psutil convergence (A219/A221).

Single Python entry point for the process/system queries that used to call
psutil directly.  Native primitives (``gptbridge_native_*`` via
``_sovereign_native``) are preferred; psutil remains a bounded transition
fallback until P24 removes it from requirements — every fallback call site
is marked ``_psutil_fallback`` so the residual is enumerable and can be
driven to zero.  When neither backend can answer, the functions return the
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
    """Any backend can answer (native preferred, bounded psutil fallback)."""
    return _native() is not None or _psutil() is not None


def _psutil() -> Any:
    try:
        import psutil  # type: ignore[import-not-found]

        return psutil
    except ImportError:  # pragma: no cover
        return None


# ---------------------------------------------------------------------------
# system memory
# ---------------------------------------------------------------------------

def system_memory_total_bytes() -> int:
    n = _native()
    if n is not None:
        v = int(n.system_memory_total_bytes())
        if v > 0:
            return v
    p = _psutil()
    if p is not None:  # _psutil_fallback
        return int(p.virtual_memory().total)
    return -1


def system_memory_available_bytes() -> int:
    n = _native()
    if n is not None:
        v = int(n.system_memory_available_bytes())
        if v > 0:
            return v
    p = _psutil()
    if p is not None:  # _psutil_fallback
        return int(p.virtual_memory().available)
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
    p = _psutil()
    if p is not None:  # _psutil_fallback
        return int(p.cpu_count(logical=True) or 0)
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
    p = _psutil()
    if p is not None:  # _psutil_fallback
        ct = p.cpu_times()
        idle = float(getattr(ct, "idle", 0.0))
        busy = float(ct.user) + float(getattr(ct, "system", 0.0))
        total = busy + idle
        if total > 0:
            # normalise to the same tuple; units don't matter (ratio only)
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
        p = _psutil()
        if p is not None:  # _psutil_fallback
            try:
                ct = p.Process(int(pid)).cpu_times()
                return float(ct.user + ct.system)
            except p.Error:
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
    p = _psutil()
    if p is not None:  # _psutil_fallback
        try:
            proc = p.Process(int(pid))
            return proc.is_running() and proc.status() != p.STATUS_ZOMBIE
        except p.Error:
            return False
    return False


def process_name(pid: int) -> Optional[str]:
    n = _native()
    if n is not None:
        v = n.process_name(int(pid))
        if v:
            return str(v)
    p = _psutil()
    if p is not None:  # _psutil_fallback
        try:
            return str(p.Process(int(pid)).name())
        except p.Error:
            return None
    return None


def process_working_set_bytes(pid: int) -> int:
    n = _native()
    if n is not None:
        v = int(n.process_working_set_bytes(int(pid)))
        if v >= 0:
            return v
    p = _psutil()
    if p is not None:  # _psutil_fallback
        try:
            return int(p.Process(int(pid)).memory_info().rss)
        except p.Error:
            return -1
    return -1


def process_private_bytes(pid: int) -> int:
    n = _native()
    if n is not None:
        v = n.process_private_bytes(int(pid))
        if v >= 0:
            return v
    p = _psutil()
    if p is not None:  # _psutil_fallback
        try:
            return int(
                getattr(p.Process(int(pid)).memory_info(), "private", -1)
            )
        except p.Error:
            return -1
    return -1



def process_list(max_count: int = 65536) -> list[int]:
    n = _native()
    if n is not None:
        v = n.process_list(int(max_count))
        if v is not None:
            return [int(x) for x in v]
    p = _psutil()
    if p is not None:  # _psutil_fallback
        return [int(x) for x in p.pids()]
    return []


def process_children(pid: int, max_count: int = 4096) -> list[int]:
    n = _native()
    if n is not None:
        v = n.process_children(int(pid), int(max_count))
        if v is not None:
            return [int(x) for x in v]
        return []
    p = _psutil()
    if p is not None:  # _psutil_fallback
        try:
            return [int(c.pid) for c in p.Process(int(pid)).children(recursive=True)]
        except p.Error:
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
    p = _psutil()
    if p is not None:  # _psutil_fallback
        try:
            p.Process(int(pid)).kill()
            return True
        except p.Error:
            return False
    return False


def process_exe(pid: int) -> Optional[str]:
    n = _native()
    if n is not None:
        v = n.process_exe(int(pid))
        if v:
            return str(v)
    p = _psutil()
    if p is not None:  # _psutil_fallback
        try:
            return str(p.Process(int(pid)).exe()) or None
        except p.Error:
            return None
    return None


def process_cmdline(pid: int) -> Optional[str]:
    """Full command line as a single string (argv joined with spaces)."""
    n = _native()
    if n is not None:
        v = n.process_cmdline(int(pid))
        if v:
            return str(v)
        return None
    p = _psutil()
    if p is not None:  # _psutil_fallback
        try:
            return " ".join(p.Process(int(pid)).cmdline()) or None
        except p.Error:
            return None
    return None


def process_ppid(pid: int) -> int:
    """Parent pid, or -1 (native PROCESSENTRY32 ppid; psutil fallback)."""
    n = _native()
    if n is not None and hasattr(n, "process_ppid"):
        return int(n.process_ppid(int(pid)))
    p = _psutil()
    if p is not None:  # _psutil_fallback
        try:
            return int(p.Process(int(pid)).ppid())
        except p.Error:
            return -1
    return -1


def process_ancestors(pid: int, max_depth: int = 32) -> list[int]:
    """Ancestor pids walking the ppid chain (bounded depth, cycle-safe)."""
    out: list[int] = []
    seen = {int(pid)}
    cur = int(pid)
    for _ in range(max(1, int(max_depth))):
        ppid = process_ppid(cur)
        if ppid <= 0 or ppid in seen:
            break
        out.append(ppid)
        seen.add(ppid)
        cur = ppid
    return out


def process_num_threads(pid: int) -> int:
    """Thread count — -1 when unavailable."""
    n = _native()
    if n is not None and hasattr(n, "process_num_threads"):
        v = int(n.process_num_threads(int(pid)))
        if v >= 0:
            return v
        return -1
    p = _psutil()
    if p is not None:  # _psutil_fallback
        try:
            return int(p.Process(int(pid)).num_threads())
        except p.Error:
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
    p = _psutil()
    if p is not None:  # _psutil_fallback
        try:
            proc = p.Process(int(pid))
            if hasattr(proc, "num_handles"):
                return int(proc.num_handles())
            if hasattr(proc, "open_files"):
                return len(proc.open_files())
        except p.Error:
            return -1
    return -1


def tcp_listen_pid(port: int) -> int:
    n = _native()
    if n is not None:
        return int(n.tcp_listen_pid(int(port)))
    p = _psutil()
    if p is not None:  # _psutil_fallback
        for conn in p.net_connections(kind="tcp"):
            if conn.laddr and conn.laddr.port == int(port) and conn.status == p.CONN_LISTEN:
                return int(conn.pid or -1)
    return -1
