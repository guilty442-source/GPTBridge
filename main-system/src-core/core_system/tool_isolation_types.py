"""Data structures for tool isolation — A266.

Provides the ToolIsolationEntry and IsolationPolicy dataclasses
used by the tool isolation manager.
"""

from __future__ import annotations

import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Any


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
    # A266: Runtime generation isolation
    runtime_generation: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    # A266: Data authority isolation - tool owns its data directories
    data_root: str = ""
    # A266: Configuration isolation - per-tool config
    config_root: str = ""
    # A266: Logs/cache isolation
    log_root: str = ""
    cache_root: str = ""
    # A266: Channel route isolation
    channel_id: str = ""
    # A266: Repair isolation
    repair_root: str = ""
    # A266: Window host isolation (for Electron tools)
    window_host_pid: int = 0
    # A266/A121: Job Object assignment verified at registration.
    job_assigned: bool = False


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
    # A266: Explicit start/stop only - no implicit autostart
    implicit_autostart: bool = False
    # A266: Repair isolation - tool repairs don't affect other tools
    repair_isolation: bool = True


__all__ = ["ToolIsolationEntry", "IsolationPolicy"]
