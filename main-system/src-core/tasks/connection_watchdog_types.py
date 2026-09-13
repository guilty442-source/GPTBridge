"""Connection watchdog — types and constants.

Extracted from connection_watchdog.py: dataclasses, constants, and
the IPC connection state writer.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from core_system.versioning import component_version

CONNECTION_WATCHDOG_VERSION: Final[str] = component_version("connection-watchdog")
CONNECTION_PROBE_INTERVAL: Final[float] = 5.0
CONNECTION_PROBE_TIMEOUT: Final[float] = 3.0
CONNECTION_DEAD_THRESHOLD: Final[int] = 2  # consecutive dead probes → disconnected
CONNECTION_STATE_FILE: Final[str] = "ipc-connection-state.json"
# Allow a single transient probe failure without counting toward the dead
# threshold — only sustained failures indicate a real disconnection.
CONNECTION_PROBE_RETRY_GRACE: Final[int] = 1


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class ConnectionSnapshot:
    """Point-in-time snapshot of the frontend-backend connection state."""

    backend_process_alive: bool = False
    backend_http_healthy: bool = False
    frontend_connected: bool = False
    overall_state: str = "unknown"  # connected, degraded, disconnected, starting
    consecutive_dead: int = 0
    last_change_at: str = ""
    probe_count: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ConnectionEvent:
    """A connection state transition event."""

    event_id: str
    timestamp: str
    from_state: str
    to_state: str
    backend_process_alive: bool
    backend_http_healthy: bool
    frontend_connected: bool
    trigger_repair: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def write_ipc_connection_state(
    project_root: Path, active_connections: int
) -> None:
    """Write IPC connection state for the watchdog to read.

    Called by the IPC server when WebSocket connections open/close.
    """
    state_file = (
        project_root / "main-system" / "runtime" / "state" / "ipc-connections.json"
    )
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "active_connections": active_connections,
            "updated_at": _iso_now(),
        }
        tmp = state_file.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        os.replace(tmp, state_file)
    except OSError:
        pass
