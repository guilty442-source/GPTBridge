"""resource executors — 資源狀態量測（stdlib：ctypes/shutil/os；無第三方）。"""

from __future__ import annotations

import ctypes
import os
import shutil
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from ..governance.delegation import ExecutorBinding

_MODEL_CATALOG: dict[str, dict[str, str]] = {
    "native-llm": {"kind": "local-native", "engine": "xingcheng-peer", "origin": "local-only"},
}


def _memory_state(payload: dict[str, Any]) -> dict[str, Any]:
    if sys.platform == "win32":
        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", wintypes.DWORD),
                ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        state = MemoryStatusEx()
        state.dwLength = ctypes.sizeof(MemoryStatusEx)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)):
            total = int(state.ullTotalPhys)
            available = int(state.ullAvailPhys)
            return {
                "memory_total_bytes": total,
                "memory_available_bytes": available,
                "memory_load_percent": int(state.dwMemoryLoad),
                "source": "ctypes-win32",
            }
        return {"memory_state": "unavailable", "source": "ctypes-win32"}
    if hasattr(os, "sysconf"):
        try:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            return {"memory_total_bytes": int(pages * page_size), "source": "posix-sysconf"}
        except (ValueError, OSError, KeyError):
            pass
    return {"memory_state": "unavailable", "source": "platform-unknown"}


def _disk_state(payload: dict[str, Any]) -> dict[str, Any]:
    target = str(payload.get("target") or os.getcwd())
    usage = shutil.disk_usage(target)
    return {
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "source": "shutil-disk-usage",
    }


def _compute_state(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "cpu_count": os.cpu_count() or 0,
        "process_count": 1,
        "local_only": True,
        "source": "os-cpu-count",
    }


def _model_state(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "catalog": {name: spec for name, spec in _MODEL_CATALOG.items()},
        "origin": "local-owned-only",
        "source": "declarative-catalog",
    }


def _provision_release(payload: dict[str, Any]) -> dict[str, Any]:
    action = str(payload.get("action") or "")
    if action == "release":
        return {"released": True, "plan": {}, "executor": "provision-release"}
    if action in ("provision", "deploy"):
        return {"provisioned": True, "plan": {}, "executor": "provision-release"}
    return {"ok": False, "reason": "unknown-action", "executor": "provision-release"}


def bindings() -> list[ExecutorBinding]:
    return [
        ExecutorBinding(
            executor_id="memory-state",
            boundary="resource-state-monitor",
            permission_intent="memory-state",
            owner_sovereign="resource",
            target="resource-sovereign:resource:memory-state",
            implementation=_memory_state,
        ),
        ExecutorBinding(
            executor_id="disk-state",
            boundary="resource-state-monitor",
            permission_intent="disk-state",
            owner_sovereign="resource",
            target="resource-sovereign:resource:disk-state",
            implementation=_disk_state,
        ),
        ExecutorBinding(
            executor_id="compute-state",
            boundary="resource-state-monitor",
            permission_intent="compute-state",
            owner_sovereign="resource",
            target="resource-sovereign:resource:compute-state",
            implementation=_compute_state,
        ),
        ExecutorBinding(
            executor_id="model-state",
            boundary="resource-state-monitor",
            permission_intent="model-state",
            owner_sovereign="resource",
            target="resource-sovereign:resource:model-state",
            implementation=_model_state,
        ),
        ExecutorBinding(
            executor_id="provision-release",
            boundary="resource-provision-release",
            permission_intent="provision-release",
            owner_sovereign="resource",
            target="resource-sovereign:resource:provision-release",
            implementation=_provision_release,
        ),
    ]


__all__ = ["bindings"]