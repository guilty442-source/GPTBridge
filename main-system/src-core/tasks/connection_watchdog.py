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
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

from core_system.versioning import component_version

CONNECTION_WATCHDOG_VERSION: Final[str] = component_version("connection-watchdog")
CONNECTION_PROBE_INTERVAL: Final[float] = 3.0
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


class ConnectionWatchdog:
    """Background thread that monitors frontend-backend connection health.

    Runs alongside boot_core's health probe loop.  When the connection
    degrades or drops, it:
    1. Records the state transition.
    2. Notifies the learning store (for pattern recognition).
    3. Records the connection outcome via CentralRepairService for learning;
       actual repair is requested through the governed repair path (A72).
    4. Writes connection state to a file for observability.
    """

    def __init__(
        self,
        project_root: Path,
        health_port: int = 8765,
        *,
        probe_interval: float = CONNECTION_PROBE_INTERVAL,
        probe_timeout: float = CONNECTION_PROBE_TIMEOUT,
        dead_threshold: int = CONNECTION_DEAD_THRESHOLD,
    ) -> None:
        self.project_root = project_root.resolve()
        self.health_port = health_port
        self.probe_interval = probe_interval
        self.probe_timeout = probe_timeout
        self.dead_threshold = dead_threshold
        self._stop = threading.Event()
        self._snapshot = ConnectionSnapshot()
        self._events: list[ConnectionEvent] = []
        self._lock = threading.Lock()
        self._state_file = (
            self.project_root / "main-system" / "runtime" / "state" / CONNECTION_STATE_FILE
        )
        self._ipc_state_file = (
            self.project_root / "main-system" / "runtime" / "state" / "ipc-connections.json"
        )
        self._learning_store: Any = None
        self._repair_callback: Any = None
        self._repair_triggered = False
        # Loopback HTTP probes must bypass any system proxy — a PAC file or
        # registry proxy would otherwise route 127.0.0.1 traffic through an
        # external proxy and fail with WinError 10061.
        self._http_opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )

    def set_repair_callback(self, callback: Any) -> None:
        """Set a callback to invoke when connection repair is needed.

        The callback receives (failure_code: str, snapshot: ConnectionSnapshot)
        and should return a dict with at least {"ok": bool}.
        """
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

    def _probe_backend_http(self) -> bool:
        """Probe the backend HTTP /health endpoint.

        The backend is considered HTTP-healthy when the core runtime is
        ready (governance + backend runtime + dependencies) and startup_dead
        is not True.  Full readiness (runtime_state=ready, including
        authenticated IPC) is the strongest signal, but the backend is also
        healthy when it is fully started and merely awaiting a frontend
        session.

        The health endpoint returns HTTP 503 while the runtime is still
        starting or when the frontend has not connected.  A 503 response
        still carries the full JSON payload, so we must read it rather than
        treating it as a connection failure.
        """
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{self.health_port}/health?brief=1",
                headers={"Connection": "close"},
            )
            try:
                response_ctx = self._http_opener.open(
                    request, timeout=self.probe_timeout
                )
            except urllib.error.HTTPError as http_error:
                if http_error.code != 503:
                    return False
                body = http_error.read().decode("utf-8")
                payload = json.loads(body)
            else:
                with response_ctx as response:
                    if not (200 <= response.status < 300):
                        return False
                    payload = json.loads(response.read().decode("utf-8"))
            if payload.get("startup_dead") is True:
                return False
            # Full readiness (frontend connected) is the strongest signal.
            if (
                payload.get("ok") is True
                and payload.get("runtime_state") == "ready"
                and payload.get("governance_ready") is True
            ):
                return True
            # Core-ready without frontend: governance + backend runtime
            # + dependencies are up, but authenticated IPC is not yet
            # connected.  This is a healthy backend awaiting a user
            # session.
            return bool(
                payload.get("governance_ready") is True
                and payload.get("backend_runtime_ready") is True
                and payload.get("dependencies_ready") is True
            )
        except (OSError, ValueError, UnicodeDecodeError, urllib.error.URLError):
            return False

    def _check_frontend_connected(self) -> bool:
        """Check if the frontend WebSocket is connected to the IPC server.

        The IPC server writes connection state to ipc-connections.json.
        If the file doesn't exist or is stale, assume disconnected.
        """
        if not self._ipc_state_file.is_file():
            return False
        try:
            data = json.loads(self._ipc_state_file.read_text(encoding="utf-8"))
            active = int(data.get("active_connections", 0))
            updated_at = str(data.get("updated_at", ""))
            # Consider stale if older than 20 seconds (aligned with the
            # backend heartbeat timeout so a dead session is detected promptly).
            if updated_at:
                from datetime import datetime as _dt
                try:
                    parsed = _dt.fromisoformat(updated_at.replace("Z", "+00:00"))
                    age = (datetime.now(timezone.utc) - parsed).total_seconds()
                    if age > 20:
                        return False
                except (ValueError, TypeError):
                    pass
            return active > 0
        except (OSError, json.JSONDecodeError, ValueError):
            return False

    def _compute_state(
        self,
        backend_alive: bool,
        backend_http: bool,
        frontend_connected: bool,
    ) -> str:
        if backend_alive and backend_http and frontend_connected:
            return "connected"
        if backend_alive and backend_http and not frontend_connected:
            return "degraded"  # backend up but frontend not connected
        if not backend_alive:
            return "disconnected"
        return "starting"

    def _record_event(
        self,
        from_state: str,
        to_state: str,
        snapshot: ConnectionSnapshot,
        *,
        trigger_repair: bool = False,
    ) -> ConnectionEvent:
        from uuid import uuid4
        event = ConnectionEvent(
            event_id=uuid4().hex,
            timestamp=_iso_now(),
            from_state=from_state,
            to_state=to_state,
            backend_process_alive=snapshot.backend_process_alive,
            backend_http_healthy=snapshot.backend_http_healthy,
            frontend_connected=snapshot.frontend_connected,
            trigger_repair=trigger_repair,
        )
        with self._lock:
            self._events.append(event)
            # Keep only the last 100 events.
            if len(self._events) > 100:
                self._events = self._events[-100:]
        # Record in learning store using the consistent connection signature
        # (failure_code, from_state->to_state, "ipc/connection") so that
        # record and lookup use the same components — closing the loop.
        if self._learning_store is not None:
            try:
                from .repair_learning import ErrorSignature, _normalize_error_signature
                failure_code = (
                    "FRONTEND_BACKEND_DISCONNECTED"
                    if to_state == "disconnected"
                    else f"CONNECTION_{to_state.upper()}"
                )
                message = f"{from_state}->{to_state}"
                sig = ErrorSignature(
                    signature_hash=_normalize_error_signature(
                        failure_code,
                        message,
                        file_path="ipc/connection",
                    ),
                    error_class=failure_code,
                    message_pattern=message,
                    failure_code=failure_code,
                    file_context="ipc/connection",
                    target_tool_id="main-system",
                )
                # Use the CentralRepairService's connection learning methods
                # if available (they close the loop with consistent signatures).
                try:
                    from .central_repair import CentralRepairService
                    repair_root = (
                        self.project_root / "main-system" / "data" / "automatic-repair"
                    )
                    repair_root.mkdir(parents=True, exist_ok=True)
                    service = CentralRepairService(self.project_root, repair_root)
                    service.record_connection_outcome(
                        failure_code,
                        from_state,
                        to_state,
                        remedy="connection-watchdog",
                        ok=to_state == "connected",
                        run_id=event.event_id,
                    )
                except Exception:
                    # Fallback: record directly in the learning store.
                    from .repair_learning import RepairOutcome
                    outcome = RepairOutcome(
                        run_id=event.event_id,
                        signature_hash=sig.signature_hash,
                        remedy="connection-watchdog",
                        ok=to_state == "connected",
                        detail=event.as_dict(),
                    )
                    self._learning_store.record_error(sig)
                    self._learning_store.record_outcome(outcome)
            except Exception:
                pass  # Learning is best-effort.
        return event

    def _write_state(self) -> None:
        """Write connection state to the state file for observability."""
        try:
            self._state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": CONNECTION_WATCHDOG_VERSION,
                "snapshot": self.snapshot.as_dict(),
                "updated_at": _iso_now(),
            }
            tmp = self._state_file.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, self._state_file)
        except OSError:
            pass

    def probe_once(
        self,
        backend_process_alive: bool | None = None,
    ) -> ConnectionSnapshot:
        """Perform one probe cycle and return the updated snapshot.

        If backend_process_alive is provided, use it; otherwise assume alive
        (boot_core calls this with the child process state).
        """
        if backend_process_alive is None:
            backend_process_alive = True  # assume alive if not specified
        backend_http = self._probe_backend_http()
        frontend_connected = self._check_frontend_connected()
        new_state = self._compute_state(
            backend_process_alive, backend_http, frontend_connected
        )
        with self._lock:
            old_state = self._snapshot.overall_state
            old_dead = self._snapshot.consecutive_dead
            if new_state == "connected":
                new_dead = 0
            elif new_state == "degraded":
                # Backend is healthy but frontend is not connected — this
                # is an expected state while waiting for a user session and
                # should not count toward the dead threshold.
                new_dead = 0
            elif old_state == "connected" and new_state == "disconnected":
                # Sudden drop from connected to disconnected is likely a
                # transient network glitch — allow one grace probe before
                # counting toward the dead threshold.
                new_dead = max(0, old_dead - CONNECTION_PROBE_RETRY_GRACE + 1)
            else:
                new_dead = old_dead + 1
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

        # Record state transition, or the first threshold crossing.
        if new_state != old_state:
            self._record_event(
                old_state, new_state, snapshot, trigger_repair=trigger
            )
        elif trigger:
            self._record_event(old_state, new_state, snapshot, trigger_repair=True)
        if trigger and self._repair_callback is not None:
            try:
                self._repair_callback("FRONTEND_BACKEND_DISCONNECTED", snapshot)
            except Exception:
                pass  # Repair signalling is best-effort.
        self._write_state()
        return snapshot

    def run(self, backend_alive_fn: Any) -> None:
        """Background loop: probe connection health periodically.

        backend_alive_fn: a callable returning bool (is backend process alive?)
        """
        while not self._stop.is_set():
            try:
                alive = bool(backend_alive_fn())
                self.probe_once(backend_process_alive=alive)
            except Exception:
                pass  # Watchdog must never crash boot_core.
            if self._stop.wait(timeout=self.probe_interval):
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
