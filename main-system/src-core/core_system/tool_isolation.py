"""Tool isolation manager — facade.

This module provides the ToolIsolationManager class.  Implementation
details live in submodules:

  * :mod:`core_system.tool_isolation_job` — Windows Job Object API.
  * :mod:`core_system.tool_isolation_types` — data structures.
  * :mod:`core_system.tool_isolation_health` — health monitoring mixin.

Provides process-level isolation for independent tools (A266 compliance).

Hardening (A266/A121/A46):
- Job assignment is verified: a failed assignment is recorded in the
  isolation audit ledger and the entry is marked ``job_unassigned`` so
  consumers can detect uncontained processes.
- Every registration, job assignment result, and unregister is appended
  to a durable ``tool-isolation-audit.jsonl`` ledger (A46 ledger-per-action).
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Final

from core_system.tool_isolation_job import (
    _KERNEL32,
    _PROCESS_SET_QUOTA,
    _PROCESS_TERMINATE,
    _assign_process_to_job,
    _create_job_object,
)
from core_system.tool_isolation_types import (
    IsolationPolicy,
    ToolIsolationEntry,
)
from core_system.tool_isolation_health import ToolIsolationHealthMixin

_logger = logging.getLogger("gptbridge.tool_isolation")

_CONFIG_RELATIVE: Final[tuple[str, ...]] = (
    "..", "..", "config", "tool-isolation-policy.json",
)

_AUDIT_LEDGER: Final[Path] = (
    Path(__file__).resolve().parents[2]
    / "runtime" / "state" / "tool-isolation-audit.jsonl"
)
_AUDIT_LOCK = threading.Lock()


def _iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _manifest_independent_tool(tool_dir: Path | None) -> bool:
    """Independent tools declare ``main_system_independent_tool: true``.

    The lifecycle contract (see ``main_shutdown``): independent standalone
    tools run in their own processes, manage their own lifecycle, and must
    NOT be force-closed when the backend exits.  ``register_tool`` binds
    that manifest flag to the Job Object below (no kill-on-close), so a
    backend restart or generation handover can never terminate them.
    """
    if tool_dir is None:
        return False
    try:
        manifest = json.loads(
            (tool_dir / "manifest.json").read_text(encoding="utf-8")
        )
        return manifest.get("main_system_independent_tool") is True
    except (OSError, json.JSONDecodeError):
        return False


def _record_isolation_audit(
    *, tool_id: str, event: str, detail: dict[str, Any] | None = None
) -> None:
    """Append one isolation event to the durable ledger (A46)."""
    entry = {
        "timestamp": _iso_now(),
        "tool_id": str(tool_id),
        "event": str(event),
        "detail": dict(detail) if detail else {},
    }
    try:
        _AUDIT_LEDGER.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, sort_keys=True, default=str)
        with _AUDIT_LOCK, _AUDIT_LEDGER.open("a", encoding="utf-8") as handle:
            handle.write(line + os.linesep)
            handle.flush()
    except OSError:
        pass


class ToolIsolationManager(ToolIsolationHealthMixin):
    """Manages process-level isolation for all independent tools.

    Thread-safe.  Designed to be called from async code via
    ``asyncio.to_thread``.
    """

    def __init__(self, project_root: Path | str) -> None:
        self.project_root = Path(project_root).resolve()
        self._config_path = Path(__file__).resolve().parents[0].joinpath(*_CONFIG_RELATIVE)
        self._lock = threading.Lock()
        self._entries: dict[str, ToolIsolationEntry] = {}
        self._policy_cache: dict[str, Any] | None = None
        self._policy_mtime: float = 0.0
        self._monitor_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._crash_callbacks: list = []

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
            implicit_autostart=bool(merged.get("implicit_autostart", False)),
            repair_isolation=bool(merged.get("repair_isolation", True)),
        )

    def register_tool(
        self,
        tool_id: str,
        process: Any,
        policy: IsolationPolicy | None = None,
        tool_dir: Path | None = None,
    ) -> ToolIsolationEntry:
        """Register a spawned tool process for isolation management."""
        if policy is None:
            policy = self.resolve_policy(tool_id)

        project_root = self.project_root
        if tool_dir is None:
            tool_dir = project_root / "Standalone tools" / tool_id

        independent = _manifest_independent_tool(tool_dir)
        kill_on_close = policy.kill_on_job_close and not independent

        job_handle = _create_job_object(
            memory_limit_mb=policy.memory_limit_mb,
            cpu_percent=policy.cpu_percent_limit,
            kill_on_close=kill_on_close,
        )

        job_assigned = False
        if job_handle is not None:
            proc_handle = _KERNEL32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE,
                False,
                process.pid,
            ) if _KERNEL32 else None
            if proc_handle:
                job_assigned = _assign_process_to_job(job_handle, proc_handle)
                _KERNEL32.CloseHandle(proc_handle)
            if not job_assigned:
                _record_isolation_audit(
                    tool_id=tool_id, event="job-assignment-failed",
                    detail={"pid": process.pid, "memory_limit_mb": policy.memory_limit_mb},
                )
                _logger.warning(
                    "tool_isolation job_assignment_failed tool_id=%s pid=%d",
                    tool_id, process.pid,
                )
        else:
            _record_isolation_audit(
                tool_id=tool_id, event="job-object-unavailable",
                detail={"pid": process.pid},
            )

        runtime_generation = uuid.uuid4().hex[:12]

        data_root = str(project_root / "main-system" / "runtime" / "data" / "tools" / tool_id)
        config_root = str(tool_dir / "config")
        log_root = str(project_root / "main-system" / "runtime" / "logs" / "tools" / tool_id)
        cache_root = str(project_root / "main-system" / "runtime" / "temp" / "tools" / tool_id)
        channel_id = f"tool-{tool_id}"
        repair_root = str(project_root / "main-system" / "data" / "automatic-repair" / "tools" / tool_id)

        for path in (data_root, config_root, log_root, cache_root, repair_root):
            Path(path).mkdir(parents=True, exist_ok=True)

        entry = ToolIsolationEntry(
            tool_id=tool_id,
            pid=process.pid,
            process=process,
            job_handle=job_handle,
            memory_limit_mb=policy.memory_limit_mb,
            cpu_percent_limit=policy.cpu_percent_limit,
            runtime_generation=runtime_generation,
            data_root=data_root,
            config_root=config_root,
            log_root=log_root,
            cache_root=cache_root,
            channel_id=channel_id,
            repair_root=repair_root,
            job_assigned=job_assigned,
        )

        with self._lock:
            old = self._entries.get(tool_id)
            if old and old.job_handle and _KERNEL32:
                _KERNEL32.CloseHandle(old.job_handle)
            self._entries[tool_id] = entry

        _record_isolation_audit(
            tool_id=tool_id, event="register",
            detail={
                "pid": process.pid,
                "memory_limit_mb": policy.memory_limit_mb,
                "cpu_percent_limit": policy.cpu_percent_limit,
                "job_assigned": job_assigned,
                "independent": independent,
                "kill_on_close": kill_on_close,
                "network_policy": policy.network_policy,
                "filesystem_policy": policy.filesystem_policy,
                "runtime_generation": runtime_generation,
            },
        )

        _logger.info(
            "tool_isolated tool_id=%s pid=%d memory_limit=%dMB cpu_limit=%d%% "
            "job_object=%s independent=%s kill_on_close=%s network=%s filesystem=%s runtime_gen=%s",
            tool_id, process.pid, policy.memory_limit_mb, policy.cpu_percent_limit,
            bool(job_handle), independent, kill_on_close, policy.network_policy,
            policy.filesystem_policy, runtime_generation,
        )
        return entry

    def unregister_tool(self, tool_id: str) -> None:
        """Unregister a tool and release its Job Object handle."""
        with self._lock:
            entry = self._entries.pop(tool_id, None)
        if entry and entry.job_handle and _KERNEL32:
            _KERNEL32.CloseHandle(entry.job_handle)
        if entry is not None:
            _record_isolation_audit(
                tool_id=tool_id, event="unregister",
                detail={"pid": entry.pid, "job_assigned": entry.job_assigned},
            )

    def status(self) -> dict[str, Any]:
        """Return isolation manager status for health checks."""
        with self._lock:
            raw_entries = list(self._entries.items())
        entries = {
            tid: {
                "pid": e.pid,
                "memory_limit_mb": e.memory_limit_mb,
                "cpu_limit_percent": e.cpu_percent_limit,
                "restart_count": e.restart_count,
                "crashed": e.crashed,
                "quarantined": e.quarantined,
                "runtime_generation": e.runtime_generation,
                "data_root": e.data_root,
                "config_root": e.config_root,
                "log_root": e.log_root,
                "cache_root": e.cache_root,
                "channel_id": e.channel_id,
                "repair_root": e.repair_root,
                "job_assigned": e.job_assigned,
            }
            for tid, e in raw_entries
        }
        return {
            "registered_tools": len(entries),
            "tools": entries,
            "job_objects_active": sum(
                1 for _, e in raw_entries if e.job_handle is not None
            ),
        }

    def explicit_start(self, tool_id: str) -> dict[str, Any]:
        """Explicitly start a tool - called only on user request (A266)."""
        with self._lock:
            entry = self._entries.get(tool_id)
        if entry is None:
            return {"tool_id": tool_id, "action": "not_registered", "started": False}
        if entry.crashed and entry.quarantined:
            return {"tool_id": tool_id, "action": "quarantined", "started": False}
        return {"tool_id": tool_id, "action": "already_running", "started": True}

    def explicit_stop(self, tool_id: str, timeout: float | None = None) -> dict[str, Any]:
        """Explicitly stop a tool - called only on user request (A266)."""
        return self.shutdown_tool(tool_id, timeout)

    def isolate_repair(self, tool_id: str) -> dict[str, Any]:
        """Isolate repair for a specific tool (A266 repair isolation)."""
        with self._lock:
            entry = self._entries.get(tool_id)
        if entry is None:
            return {"tool_id": tool_id, "action": "not_registered", "isolated": False}
        repair_scope = {
            "tool_id": tool_id,
            "repair_root": entry.repair_root,
            "data_root": entry.data_root,
            "runtime_generation": entry.runtime_generation,
            "channel_id": entry.channel_id,
        }
        return {"tool_id": tool_id, "action": "repair_isolated", "isolated": True, "scope": repair_scope}

    def get_repair_scope(self, tool_id: str) -> dict[str, Any] | None:
        """Get the repair scope for a tool."""
        with self._lock:
            entry = self._entries.get(tool_id)
        if entry is None:
            return None
        return {
            "tool_id": tool_id,
            "repair_root": entry.repair_root,
            "data_root": entry.data_root,
            "runtime_generation": entry.runtime_generation,
            "channel_id": entry.channel_id,
        }


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
