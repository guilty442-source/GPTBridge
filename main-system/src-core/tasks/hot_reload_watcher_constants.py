"""Hot-reload watcher constants and data structures.

Provides the constants, ChannelHealth dataclass, and helper functions
used by the hot-reload watcher submodules.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Optional

from core_system.hot_update_service import PROTECTED_MODULE_PREFIXES

POLL_INTERVAL_SECONDS: Final[float] = 10.0
QUIET_WINDOW_SECONDS: Final[float] = 2.0
MIN_RELOAD_INTERVAL_SECONDS: Final[float] = 10.0
MAX_RELOADS_PER_MINUTE: Final[int] = 3
FAILURE_BACKOFF_SECONDS: Final[float] = 60.0
# Enhanced stability constants
MAX_CONSECUTIVE_FAILURES: Final[int] = 3
HEALTH_CHECK_INTERVAL_SECONDS: Final[float] = 60.0
CHANNEL_HEALTH_TIMEOUT_SECONDS: Final[float] = 20.0
MAX_RETRY_ATTEMPTS: Final[int] = 3
RETRY_BASE_DELAY_SECONDS: Final[float] = 5.0
RETRY_MAX_DELAY_SECONDS: Final[float] = 120.0
RETRY_JITTER_FACTOR: Final[float] = 0.3
IPC_RECONNECT_DELAY_SECONDS: Final[float] = 10.0
# P7 hard caps: the loop tick must never stall indefinitely — a hung
# prepare_generation thread or channel probe previously froze the
# watcher forever (_in_flight stuck, no further scans).
RELOAD_DEADLINE_SECONDS: Final[float] = 180.0
HEALTH_CHECK_DEADLINE_SECONDS: Final[float] = 30.0
PENDING_PATHS_CAP: Final[int] = 8192

# Only main-system code is reloaded.
WATCH_ROOTS: Final[tuple[str, ...]] = ("main-system/src-core",)

_EXCLUDED_TOKEN_DIRS: Final[tuple[str, ...]] = (
    "main-system/runtime",
    "main-system/data",
)


@dataclass
class ChannelHealth:
    """Health status of the update delivery channel."""
    is_healthy: bool = True
    last_check: float = field(default_factory=time.monotonic)
    consecutive_failures: int = 0
    last_error: Optional[str] = None
    last_success: Optional[float] = None
    ipc_connected: bool = False
    governance_reachable: bool = False

    def record_success(self) -> None:
        self.is_healthy = True
        self.consecutive_failures = 0
        self.last_error = None
        self.last_success = time.monotonic()
        self.last_check = time.monotonic()

    def record_failure(self, error: str) -> None:
        self.consecutive_failures += 1
        self.last_error = error
        self.last_check = time.monotonic()
        if self.consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
            self.is_healthy = False

    def check_timeout(self) -> bool:
        """Check if channel health has timed out."""
        return (time.monotonic() - self.last_check) > CHANNEL_HEALTH_TIMEOUT_SECONDS


def _is_protected(module_name: str) -> bool:
    return any(
        module_name == prefix or module_name.startswith(prefix + ".")
        for prefix in PROTECTED_MODULE_PREFIXES
    )


__all__ = [
    "POLL_INTERVAL_SECONDS",
    "QUIET_WINDOW_SECONDS",
    "MIN_RELOAD_INTERVAL_SECONDS",
    "MAX_RELOADS_PER_MINUTE",
    "FAILURE_BACKOFF_SECONDS",
    "MAX_CONSECUTIVE_FAILURES",
    "HEALTH_CHECK_INTERVAL_SECONDS",
    "CHANNEL_HEALTH_TIMEOUT_SECONDS",
    "MAX_RETRY_ATTEMPTS",
    "RETRY_BASE_DELAY_SECONDS",
    "RETRY_MAX_DELAY_SECONDS",
    "RETRY_JITTER_FACTOR",
    "IPC_RECONNECT_DELAY_SECONDS",
    "WATCH_ROOTS",
    "ChannelHealth",
    "_is_protected",
    "_EXCLUDED_TOKEN_DIRS",
]
