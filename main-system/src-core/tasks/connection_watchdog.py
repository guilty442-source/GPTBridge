"""Connection watchdog — monitors frontend-backend connection health and records learning.

Architecture:

  boot_core spawns backend (main.py --serve)
    └─ IPC server listens on ws://127.0.0.1:8765
        └─ Frontend (Electron) connects via WebSocket
            └─ ConnectionWatchdog monitors the link

  Health layers:
    1. Backend process alive (boot_core supervises)
    2. Backend HTTP /health returns ready with governance_ready=true (boot_core probes every 3s)
    3. Frontend WebSocket connected (IPC server tracks active connections)
    4. ConnectionWatchdog polls all three and records state transitions

  When the connection degrades:
    - Frontend disconnects → useBackendSocket schedules reconnect with backoff
    - ConnectionWatchdog detects sustained disconnection (2 dead probes)
    - ConnectionWatchdog records the outage and learns from it
    - Repair is requested through the governed repair path (A72)

  This module runs inside boot_core as a background thread.  It:
    - Polls backend /health every CONNECTION_PROBE_INTERVAL seconds
    - Polls IPC connection state file every CONNECTION_PROBE_INTERVAL seconds
    - Records state transitions to the learning store
    - Records the connection outcome via CentralRepairService for learning only;
      actual repair is requested through the governed repair path (A72).
    - Writes connection state to boot-core.json for observability

Enhanced with:
- Exponential backoff on consecutive failures
- Health check result caching with TTL
- Detailed error classification
- Resource leak detection
- Circuit breaker pattern for external dependencies

The module is decomposed into single-responsibility sub-modules (A430):
  * ``connection_watchdog_types`` — dataclasses, constants, IPC state writer
  * ``connection_watchdog_probes`` — ProbeResult, HealthCheckCache, CircuitBreaker
  * ``connection_watchdog_mixins`` — ConnectionProbeMixin, ConnectionAuditMixin
"""

from __future__ import annotations

import threading
import urllib.request
from collections import deque
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from .connection_watchdog_types import (
    CONNECTION_DEAD_THRESHOLD,
    CONNECTION_PROBE_INTERVAL,
    CONNECTION_PROBE_RETRY_GRACE,
    CONNECTION_PROBE_TIMEOUT,
    CONNECTION_STATE_FILE,
    CONNECTION_WATCHDOG_VERSION,
    ConnectionEvent,
    ConnectionSnapshot,
    _iso_now,
    write_ipc_connection_state,
)
from .connection_watchdog_probes import (
    CircuitBreaker,
    HealthCheckCache,
    ResourceUsageSnapshot,
)
from .connection_watchdog_mixins import ConnectionAuditMixin, ConnectionProbeMixin


class ConnectionWatchdog(
    ConnectionProbeMixin,
    ConnectionAuditMixin,
):
    """Background thread that monitors frontend-backend connection health.

    Runs alongside boot_core's health probe loop.  When the connection
    degrades or drops, it:
    1. Records the state transition.
    2. Notifies the learning store (for pattern recognition).
    3. Records the connection outcome via CentralRepairService for learning;
       actual repair is requested through the governed repair path (A72).
    4. Writes connection state to a file for observability.

    Enhanced with:
    - Exponential backoff on consecutive failures
    - Health check result caching with TTL
    - Detailed error classification
    - Resource leak detection
    - Circuit breaker pattern for external dependencies
    """

    def __init__(
        self,
        project_root: Path,
        health_port: int = 8765,
        *,
        probe_interval: float = CONNECTION_PROBE_INTERVAL,
        probe_timeout: float = CONNECTION_PROBE_TIMEOUT,
        dead_threshold: int = CONNECTION_DEAD_THRESHOLD,
        enable_resource_monitoring: bool = True,
        resource_check_interval_seconds: float = 60.0,
    ) -> None:
        self.project_root = project_root.resolve()
        self.health_port = health_port
        self.probe_interval = probe_interval
        self.probe_timeout = probe_timeout
        self.dead_threshold = dead_threshold
        self._stop = threading.Event()
        self._snapshot = ConnectionSnapshot()
        self._events: list[ConnectionEvent] = []
        self._lock = threading.RLock()
        self._state_file = (
            self.project_root / "main-system" / "runtime" / "state" / CONNECTION_STATE_FILE
        )
        self._ipc_state_file = (
            self.project_root / "main-system" / "runtime" / "state" / "ipc-connections.json"
        )
        self._learning_store: Any = None
        self._repair_callback: Any = None
        self._repair_triggered = False
        self._last_fault: tuple[str, str, str] | None = None
        self._http_opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )
        self._adaptive_probe_interval = probe_interval
        self._min_probe_interval = probe_interval
        self._max_probe_interval = 60.0
        self._consecutive_stable = 0
        self._consecutive_failures = 0
        self._http_cache = HealthCheckCache(ttl_seconds=5.0)
        self._ipc_cache = HealthCheckCache(ttl_seconds=5.0)
        self._http_circuit_breaker = CircuitBreaker("http-health", failure_threshold=3, recovery_timeout_seconds=15.0)
        self._ipc_circuit_breaker = CircuitBreaker("ipc-health", failure_threshold=3, recovery_timeout_seconds=15.0)
        self.enable_resource_monitoring = enable_resource_monitoring
        self.resource_check_interval = resource_check_interval_seconds
        self._resource_history: deque = deque(maxlen=100)
        self._last_resource_check = 0.0
        self._last_error: Optional[str] = None
        self._error_counts: dict[str, int] = {}
        self._last_error_time = 0.0

    def set_repair_callback(self, callback: Any) -> None:
        """Set a callback to invoke when connection repair is needed."""
        self._repair_callback = callback

    def set_learning_store(self, store: Any) -> None:
        """Set the RepairLearningStore for recording connection events."""
        self._learning_store = store

    @property
    def snapshot(self) -> ConnectionSnapshot:
        with self._lock:
            return ConnectionSnapshot(**asdict(self._snapshot))

    @property
    def events(self) -> list[ConnectionEvent]:
        with self._lock:
            return list(self._events)

    def _compute_dead_count(self, new_state: str, old_state: str, old_dead: int) -> int:
        """Compute the new consecutive-dead count for a state transition."""
        if new_state in ("connected", "degraded"):
            return 0
        if old_state == "connected" and new_state == "disconnected":
            return max(0, old_dead - CONNECTION_PROBE_RETRY_GRACE + 1)
        return old_dead + 1

    def probe_once(
        self,
        backend_process_alive: bool | None = None,
    ) -> ConnectionSnapshot:
        """Perform one probe cycle and return the updated snapshot."""
        if backend_process_alive is None:
            backend_process_alive = True
        backend_http = self._probe_backend_http()
        frontend_connected = self._check_frontend_connected()
        new_state = self._compute_state(
            backend_process_alive, backend_http, frontend_connected
        )
        with self._lock:
            old_state = self._snapshot.overall_state
            new_dead = self._compute_dead_count(
                new_state, old_state, self._snapshot.consecutive_dead
            )
            self._snapshot = ConnectionSnapshot(
                backend_process_alive=backend_process_alive,
                backend_http_healthy=backend_http,
                frontend_connected=frontend_connected,
                overall_state=new_state,
                consecutive_dead=new_dead,
                last_change_at=_iso_now() if new_state != old_state else self._snapshot.last_change_at,
                probe_count=self._snapshot.probe_count + 1,
            )
            snapshot = self._snapshot
        trigger = (
            new_state != "connected"
            and new_dead >= self.dead_threshold
            and not self._repair_triggered
        )
        if new_state == "connected":
            self._repair_triggered = False
        elif trigger:
            self._repair_triggered = True
        if new_state != old_state:
            self._record_event(old_state, new_state, snapshot, trigger_repair=trigger)
        elif trigger:
            self._record_event(old_state, new_state, snapshot, trigger_repair=True)
        if trigger and self._repair_callback is not None:
            try:
                self._repair_callback("FRONTEND_BACKEND_DISCONNECTED", snapshot)
            except Exception:
                pass
        self._write_state()
        return snapshot

    def run(self, backend_alive_fn: Any) -> None:
        """Background loop: probe connection health periodically with adaptive interval."""
        while not self._stop.is_set():
            try:
                alive = bool(backend_alive_fn())
                snapshot = self.probe_once(backend_process_alive=alive)
                if snapshot.overall_state == "connected":
                    self._consecutive_stable += 1
                    if self._consecutive_stable >= 3:
                        self._adaptive_probe_interval = min(
                            self._adaptive_probe_interval * 1.5,
                            self._max_probe_interval,
                        )
                else:
                    self._consecutive_stable = 0
                    self._adaptive_probe_interval = self._min_probe_interval
            except Exception:
                pass
            if self._stop.wait(timeout=self._adaptive_probe_interval):
                break

    def stop(self) -> None:
        self._stop.set()

    def get_status(self) -> dict[str, Any]:
        """Return full connection watchdog status for observability."""
        with self._lock:
            snapshot = ConnectionSnapshot(**asdict(self._snapshot))
            events = list(self._events[-10:])
        return {
            "version": CONNECTION_WATCHDOG_VERSION,
            "snapshot": snapshot.as_dict(),
            "recent_events": [e.as_dict() for e in events],
            "probe_interval_seconds": self.probe_interval,
            "dead_threshold": self.dead_threshold,
        }


__all__ = [
    "CONNECTION_DEAD_THRESHOLD",
    "CONNECTION_PROBE_INTERVAL",
    "CONNECTION_PROBE_TIMEOUT",
    "CONNECTION_WATCHDOG_VERSION",
    "ConnectionEvent",
    "ConnectionSnapshot",
    "ConnectionWatchdog",
    "write_ipc_connection_state",
]
