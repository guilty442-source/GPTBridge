"""Windows Job Object API for tool isolation — ctypes bindings.

Provides the low-level Windows Job Object API wrappers used by the
tool isolation manager for resource limits and crash containment.
"""

from __future__ import annotations

import ctypes
import os
from typing import Any

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


__all__ = [
    "_KERNEL32",
    "_PROCESS_SET_QUOTA",
    "_PROCESS_TERMINATE",
    "_create_job_object",
    "_assign_process_to_job",
]
