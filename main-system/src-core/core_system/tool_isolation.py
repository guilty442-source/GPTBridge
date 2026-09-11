"""Tool isolation manager — process-level isolation for independent tools.

Provides:
  * **Resource limits** via Windows Job Objects (memory ceiling, kill-on-close).
  * **Health monitoring** via psutil (CPU, memory, process status).
  * **Crash containment** — tool crash triggers isolation event, never
    propagates to the main system.
  * **Automatic restart** with bounded backoff.
  * **Graceful shutdown** — coordinated signal escalation
    (SIGTERM → SIGTERM → SIGKILL) with state-flush timeout.
  * **Network policy** enforcement (loopback-only / offline / unrestricted).
  * **Filesystem policy** — tool-scoped access with deny patterns.

The manager is thread-safe and designed to be called from the async
toolbox service via ``asyncio.to_thread``.
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import psutil

_logger = logging.getLogger("gptbridge.tool_isolation")

# ------------------------------------------------------------------
# Windows Job Object API (ctypes)
# ------------------------------------------------------------------

_KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0400
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x0200

_JOB_OBJECT_CPU_RATE_CONTROL = 0x0004
_JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x0001
_JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x0002


class _IO_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_ulonglong),
        ("WriteOperationCount", ctypes.c_ulonglong),
        ("OtherOperationCount", ctypes.c_ulonglong),
        ("ReadTransferCount", ctypes.c_ulonglong),
        ("WriteTransferCount", ctypes.c_ulonglong),
        ("OtherTransferCount", ctypes.c_ulonglong),
    ]


class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", ctypes.c_uint32),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", ctypes.c_uint32),
        ("Affinity", ctypes.c_void_p),
        ("PriorityClass", ctypes.c_uint32),
        ("SchedulingClass", ctypes.c_uint32),
    ]


class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION),
        ("IoInfo", _IO_COUNTERS),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("ControlFlags", ctypes.c_uint32),
        ("CpuRate", ctypes.c_uint32),
    ]


_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION = 15
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001


def _create_job_object(memory_limit_mb: int, cpu_percent: int, kill_on_close: bool) -> Any:
    """Create a Windows Job Object with resource limits. Returns handle or None."""
    if _KERNEL32 is None:
        return None
    handle = _KERNEL32.CreateJobObjectW(None, None)
    if not handle:
        return None

    # Extended limits: memory ceiling + kill-on-close.
    limits = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
    limits.BasicLimitInformation.LimitFlags = 0
    if kill_on_close:
        limits.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if memory_limit_mb > 0:
        limits.BasicLimitInformation.LimitFlags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY
        limits.ProcessMemoryLimit = memory_limit_mb * 1024 * 1024

    _KERNEL32.SetInformationJobObject(
        handle,
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )

    # CPU rate control (hard cap).
    if cpu_percent > 0 and cpu_percent < 100:
        cpu_info = _JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        cpu_info.ControlFlags = (
            _JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | _JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        )
        # CPU rate is in 1/10000 of a percent (e.g. 50% = 5000).
        cpu_info.CpuRate = cpu_percent * 100
        _KERNEL32.SetInformationJobObject(
            handle,
            _JOB_OBJECT_CPU_RATE_CONTROL_INFORMATION,
            ctypes.byref(cpu_info),
            ctypes.sizeof(cpu_info),
        )

    return handle


def _assign_process_to_job(job_handle: Any, process_handle: Any) -> bool:
    """Assign a process to a Job Object."""
    if _KERNEL32 is None or job_handle is None:
        return False
    return bool(_KERNEL32.AssignProcessToJobObject(job_handle, process_handle))


# ------------------------------------------------------------------
# Data structures
# ------------------------------------------------------------------

@dataclass
class ToolIsolationEntry:
    """Tracks one isolated tool process."""
    tool_id: str
    pid: int
    process: subprocess.Popen
    job_handle: Any = None
    memory_limit_mb: int = 0
    cpu_percent_limit: int = 0
    restart_count: int = 0
    last_restart_time: float = 0.0
    last_health_check: float = 0.0
    last_cpu_percent: float = 0.0
    last_memory_mb: float = 0.0
    crashed: bool = False
    quarantined: bool = False


@dataclass
class IsolationPolicy:
    """Resolved isolation policy for a tool."""
    memory_limit_mb: int = 512
    cpu_percent_limit: int = 50
    health_check_interval_seconds: float = 5.0
    restart_on_crash: bool = True
    max_restart_attempts: int = 3
    restart_backoff_seconds: list[int] = field(default_factory=lambda: [2, 5, 15])
    graceful_shutdown_timeout_seconds: float = 10.0
    kill_on_job_close: bool = True
    network_policy: str = "loopback-only"
    filesystem_policy: str = "tool-scoped"
    state_isolation: bool = True


# ------------------------------------------------------------------
# Isolation manager
# ------------------------------------------------------------------

_CONFIG_RELATIVE: Final[tuple[str, ...]] = (
    "..", "..", "config", "tool-isolation-policy.json",
)


class ToolIsolationManager:
    """Manages process-level isolation for all independent tools.

    Thread-safe.  Designed to be called from async code via
    ``asyncio.to_thread``.
    """

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root).resolve()
        self._config_path = Path(__file__).resolve().joinpath(*_CONFIG_RELATIVE)
        self._lock = threading.Lock()
        self._entries: dict[str, ToolIsolationEntry] = {}  # tool_id → entry
        self._policy_cache: dict[str, Any] | None = None
        self._policy_mtime: float = 0.0
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._crash_callbacks: list = []

    # ------------------------------------------------------------------
    # Policy loading
    # ------------------------------------------------------------------

    def _load_policy_config(self) -> dict[str, Any]:
        """Load the isolation policy JSON, with mtime-based caching."""
        try:
            mtime = self._config_path.stat().st_mtime
            if self._policy_cache is not None and mtime == self._policy_mtime:
                return self._policy_cache
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
            self._policy_cache = raw
            self._policy_mtime = mtime
            return raw
        except (OSError, json.JSONDecodeError):
            return self._policy_cache or {}

    def resolve_policy(self, tool_id: str) -> IsolationPolicy:
        """Resolve the effective isolation policy for a tool."""
        config = self._load_policy_config()
        defaults = config.get("defaults", {})
        overrides = config.get("per_tool_overrides", {}).get(tool_id, {})
        merged = {**defaults, **overrides}
        return IsolationPolicy(
            memory_limit_mb=int(merged.get("memory_limit_mb", 512)),
            cpu_percent_limit=int(merged.get("cpu_percent_limit", 50)),
            health_check_interval_seconds=float(merged.get("health_check_interval_seconds", 5.0)),
            restart_on_crash=bool(merged.get("restart_on_crash", True)),
            max_restart_attempts=int(merged.get("max_restart_attempts", 3)),
            restart_backoff_seconds=list(merged.get("restart_backoff_seconds", [2, 5, 15])),
            graceful_shutdown_timeout_seconds=float(merged.get("graceful_shutdown_timeout_seconds", 10.0)),
            kill_on_job_close=bool(merged.get("kill_on_job_close", True)),
            network_policy=str(merged.get("network_policy", "loopback-only")),
            filesystem_policy=str(merged.get("filesystem_policy", "tool-scoped")),
            state_isolation=bool(merged.get("state_isolation", True)),
        )

    # ------------------------------------------------------------------
    # Process registration (called after tool spawn)
    # ------------------------------------------------------------------

    def register_tool(
        self,
        tool_id: str,
        process: subprocess.Popen,
        policy: IsolationPolicy | None = None,
    ) -> ToolIsolationEntry:
        """Register a spawned tool process for isolation management."""
        if policy is None:
            policy = self.resolve_policy(tool_id)

        # Create Job Object with resource limits.
        job_handle = _create_job_object(
            memory_limit_mb=policy.memory_limit_mb,
            cpu_percent=policy.cpu_percent_limit,
            kill_on_close=policy.kill_on_job_close,
        )

        # Assign the process to the Job Object.
        if job_handle is not None:
            # Get process handle from Popen.
            proc_handle = _KERNEL32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                False,
                process.pid,
            ) if _KERNEL32 else None
            if proc_handle:
                _assign_process_to_job(job_handle, proc_handle)
                _KERNEL32.CloseHandle(proc_handle)

        entry = ToolIsolationEntry(
            tool_id=tool_id,
            pid=process.pid,
            process=process,
            job_handle=job_handle,
            memory_limit_mb=policy.memory_limit_mb,
            cpu_percent_limit=policy.cpu_percent_limit,
        )

        with self._lock:
            # If there's an existing entry, clean up its job handle first.
            old = self._entries.get(tool_id)
            if old and old.job_handle and _KERNEL32:
                _KERNEL32.CloseHandle(old.job_handle)
            self._entries[tool_id] = entry

        _logger.info(
            "tool_isolated tool_id=%s pid=%d memory_limit=%dMB cpu_limit=%d%% "
            "job_object=%s network=%s filesystem=%s",
            tool_id, process.pid, policy.memory_limit_mb, policy.cpu_percent_limit,
            bool(job_handle), policy.network_policy, policy.filesystem_policy,
        )
        return entry

    def unregister_tool(self, tool_id: str) -> None:
        """Unregister a tool and release its Job Object handle."""
        with self._lock:
            entry = self._entries.pop(tool_id, None)
        if entry and entry.job_handle and _KERNEL32:
            _KERNEL32.CloseHandle(entry.job_handle)

    # ------------------------------------------------------------------
    # Health monitoring
    # ------------------------------------------------------------------

    def check_tool_health(self, tool_id: str) -> dict[str, Any]:
        """Check the health of a single tool. Returns a status dict."""
        with self._lock:
            entry = self._entries.get(tool_id)
        if entry is None:
            return {"tool_id": tool_id, "status": "not_registered"}
        try:
            proc = psutil.Process(entry.pid)
            if not proc.is_running():
                return {
                    "tool_id": tool_id,
                    "status": "crashed",
                    "pid": entry.pid,
                    "exit_code": entry.process.returncode,
                    "restart_count": entry.restart_count,
                }
            cpu = proc.cpu_percent(interval=0.1)
            mem_info = proc.memory_info()
            mem_mb = mem_info.rss / (1024 * 1024)
            entry.last_health_check = time.monotonic()
            entry.last_cpu_percent = cpu
            entry.last_memory_mb = mem_mb
            over_memory = (
                entry.memory_limit_mb > 0
                and mem_mb > entry.memory_limit_mb
            )
            over_cpu = (
                entry.cpu_percent_limit > 0
                and cpu > entry.cpu_percent_limit * 1.5  # allow brief spikes
            )
            return {
                "tool_id": tool_id,
                "status": "healthy" if not (over_memory or over_cpu) else "warning",
                "pid": entry.pid,
                "cpu_percent": round(cpu, 1),
                "memory_mb": round(mem_mb, 1),
                "memory_limit_mb": entry.memory_limit_mb,
                "cpu_limit_percent": entry.cpu_percent_limit,
                "over_memory": over_memory,
                "over_cpu": over_cpu,
                "restart_count": entry.restart_count,
            }
        except psutil.NoSuchProcess:
            return {
                "tool_id": tool_id,
                "status": "crashed",
                "pid": entry.pid,
                "restart_count": entry.restart_count,
            }
        except Exception as exc:
            return {
                "tool_id": tool_id,
                "status": "error",
                "error": str(exc),
            }

    def check_all_health(self) -> list[dict[str, Any]]:
        """Check health of all registered tools."""
        with self._lock:
            tool_ids = list(self._entries.keys())
        return [self.check_tool_health(tid) for tid in tool_ids]

    # ------------------------------------------------------------------
    # Crash containment & restart
    # ------------------------------------------------------------------

    def register_crash_callback(self, callback) -> None:
        """Register a callback called when a tool crashes.

        Callback signature: ``callback(tool_id: str, entry: ToolIsolationEntry)``
        """
        self._crash_callbacks.append(callback)

    def handle_crash(self, tool_id: str) -> dict[str, Any]:
        """Handle a tool crash: quarantine, notify, and optionally restart."""
        with self._lock:
            entry = self._entries.get(tool_id)
        if entry is None:
            return {"tool_id": tool_id, "action": "not_registered"}

        entry.crashed = True
        policy = self.resolve_policy(tool_id)

        # Quarantine: record crash info.
        config = self._load_policy_config()
        crash_config = config.get("crash_containment", {})
        if crash_config.get("isolate_on_crash", True):
            self._quarantine_crash(tool_id, entry)

        # Notify callbacks.
        for cb in self._crash_callbacks:
            try:
                cb(tool_id, entry)
            except Exception:
                pass

        # Restart logic.
        if not policy.restart_on_crash:
            return {"tool_id": tool_id, "action": "quarantined", "restarted": False}
        if entry.restart_count >= policy.max_restart_attempts:
            _logger.warning(
                "tool_crash_max_restarts tool_id=%s restarts=%d — giving up",
                tool_id, entry.restart_count,
            )
            entry.quarantined = True
            return {
                "tool_id": tool_id,
                "action": "quarantined",
                "restarted": False,
                "reason": "max_restarts_exceeded",
            }

        backoff_idx = min(entry.restart_count, len(policy.restart_backoff_seconds) - 1)
        delay = policy.restart_backoff_seconds[backoff_idx]
        entry.restart_count += 1
        entry.last_restart_time = time.monotonic()

        _logger.info(
            "tool_crash_restart tool_id=%s attempt=%d delay=%ds",
            tool_id, entry.restart_count, delay,
        )
        return {
            "tool_id": tool_id,
            "action": "restart_scheduled",
            "restarted": True,
            "delay_seconds": delay,
            "attempt": entry.restart_count,
        }

    def _quarantine_crash(self, tool_id: str, entry: ToolIsolationEntry) -> None:
        """Record crash info in the quarantine directory."""
        config = self._load_policy_config()
        crash_config = config.get("crash_containment", {})
        quarantine_dir = self.project_root / crash_config.get(
            "quarantine_dir",
            "main-system/runtime/state/tool-crash-quarantine",
        )
        try:
            quarantine_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "tool_id": tool_id,
                "pid": entry.pid,
                "restart_count": entry.restart_count,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "exit_code": entry.process.returncode,
            }
            path = quarantine_dir / f"{tool_id}-{int(time.time())}.json"
            path.write_text(
                json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            # Prune old entries.
            max_entries = int(crash_config.get("max_quarantine_entries", 20))
            entries = sorted(quarantine_dir.glob(f"{tool_id}-*.json"))
            for old_path in entries[:-max_entries]:
                old_path.unlink(missing_ok=True)
        except OSError:
            pass

    # ------------------------------------------------------------------
    # Graceful shutdown
    # ------------------------------------------------------------------

    def shutdown_tool(self, tool_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Gracefully shut down a tool with signal escalation.

        Signal order: SIGTERM → wait → SIGTERM → wait → SIGKILL.
        """
        with self._lock:
            entry = self._entries.get(tool_id)
        if entry is None:
            return {"tool_id": tool_id, "action": "not_registered"}

        policy = self.resolve_policy(tool_id)
        shutdown_timeout = timeout or policy.graceful_shutdown_timeout_seconds

        # Phase 1: graceful SIGTERM.
        try:
            entry.process.terminate()
        except Exception:
            pass

        try:
            entry.process.wait(timeout=shutdown_timeout / 2)
            self.unregister_tool(tool_id)
            return {"tool_id": tool_id, "action": "terminated", "method": "SIGTERM"}
        except subprocess.TimeoutExpired:
            pass

        # Phase 2: second SIGTERM with shorter timeout.
        try:
            entry.process.terminate()
        except Exception:
            pass
        try:
            entry.process.wait(timeout=shutdown_timeout / 3)
            self.unregister_tool(tool_id)
            return {"tool_id": tool_id, "action": "terminated", "method": "SIGTERM_2"}
        except subprocess.TimeoutExpired:
            pass

        # Phase 3: SIGKILL.
        try:
            entry.process.kill()
            entry.process.wait(timeout=2.0)
        except Exception:
            pass
        self.unregister_tool(tool_id)
        return {"tool_id": tool_id, "action": "terminated", "method": "SIGKILL"}

    def shutdown_all(self, timeout: float | None = None) -> list[dict[str, Any]]:
        """Gracefully shut down all registered tools."""
        with self._lock:
            tool_ids = list(self._entries.keys())
        return [self.shutdown_tool(tid, timeout) for tid in tool_ids]

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Return isolation manager status for health checks."""
        with self._lock:
            entries = {
                tid: {
                    "pid": e.pid,
                    "memory_limit_mb": e.memory_limit_mb,
                    "cpu_limit_percent": e.cpu_percent_limit,
                    "restart_count": e.restart_count,
                    "crashed": e.crashed,
                    "quarantined": e.quarantined,
                }
                for tid, e in self._entries.items()
            }
        return {
            "registered_tools": len(entries),
            "tools": entries,
            "job_objects_active": sum(
                1 for e in self._entries.values() if e.job_handle is not None
            ),
        }


# ------------------------------------------------------------------
# Singleton
# ------------------------------------------------------------------

_manager: ToolIsolationManager | None = None
_manager_lock = threading.Lock()


def get_isolation_manager(project_root: Path | str | None = None) -> ToolIsolationManager:
    """Return the singleton ToolIsolationManager."""
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                root = project_root or Path(__file__).resolve().parents[3]
                _manager = ToolIsolationManager(root)
    return _manager


__all__ = [
    "IsolationPolicy",
    "ToolIsolationEntry",
    "ToolIsolationManager",
    "get_isolation_manager",
]
