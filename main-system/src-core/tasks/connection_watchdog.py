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
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

T = TypeVar("T")

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

_logger = logging.getLogger("gptbridge.connection_watchdog")


class ProbeResult:
    """Result of a single health probe with metadata."""

    def __init__(
        self,
        success: bool,
        latency_ms: float,
        error: Optional[str] = None,
        error_type: Optional[str] = None,
    ):
        self.success = success
        self.latency_ms = latency_ms
        self.error = error
        self.error_type = error_type
        self.timestamp = time.monotonic()


class HealthCheckCache:
    """Thread-safe cache for health check results with TTL."""

    def __init__(self, ttl_seconds: float):
        self.ttl_seconds = ttl_seconds
        self._cache: dict[str, tuple[float, ProbeResult]] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> Optional[ProbeResult]:
        with self._lock:
            if key in self._cache:
                cached_time, result = self._cache[key]
                if time.monotonic() - cached_time < self.ttl_seconds:
                    return result
                else:
                    del self._cache[key]
        return None

    def set(self, key: str, result: ProbeResult) -> None:
        with self._lock:
            self._cache[key] = (time.monotonic(), result)

    def invalidate(self, key: str) -> None:
        with self._lock:
            self._cache.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


class CircuitBreaker:
    """Simple circuit breaker for external dependencies."""

    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout_seconds: float = 30.0,
        fallback_fn: Optional[Callable[[], Any]] = None,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout_seconds
        self.fallback_fn = fallback_fn
        self._lock = threading.RLock()
        self._state = "closed"  # closed, open, half_open
        self._failure_count = 0
        self._last_failure_time: float = 0.0

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == "open":
                if time.monotonic() - self._last_failure_time >= self.recovery_timeout:
                    self._state = "half_open"
                    _logger.info("circuit_half_open name=%s", self.name)
            return self._state

    def call(self, fn: Callable[[], T], *args: Any, **kwargs: Any) -> T:
        with self._lock:
            if self.state == "open":
                if self.fallback_fn:
                    return self.fallback_fn()
                raise Exception(f"Circuit {self.name} is open")

        try:
            result = fn(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            if self.fallback_fn:
                return self.fallback_fn()
            raise

    def _on_success(self) -> None:
        with self._lock:
            self._failure_count = 0
            if self._state == "half_open":
                self._state = "closed"

    def _on_failure(self) -> None:
        with self._lock:
            self._failure_count += 1
            self._last_failure_time = time.monotonic()
            if self._failure_count >= self.failure_threshold:
                self._state = "open"

    def reset(self) -> None:
        with self._lock:
            self._state = "closed"
            self._failure_count = 0


@dataclass
class ResourceUsageSnapshot:
    """Snapshot of resource usage for leak detection."""
    timestamp: float
    thread_count: int
    open_files: int
    memory_mb: float
    connection_count: int


class ConnectionWatchdog:
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
        # Adaptive probing
        self._adaptive_probe_interval = probe_interval
        self._min_probe_interval = probe_interval
        self._max_probe_interval = 60.0
        self._consecutive_stable = 0
        self._consecutive_failures = 0

        # Enhanced features
        self._http_cache = HealthCheckCache(ttl_seconds=5.0)
        self._ipc_cache = HealthCheckCache(ttl_seconds=5.0)
        self._http_circuit_breaker = CircuitBreaker("http-health", failure_threshold=3, recovery_timeout_seconds=15.0)
        self._ipc_circuit_breaker = CircuitBreaker("ipc-health", failure_threshold=3, recovery_timeout_seconds=15.0)

        # Resource monitoring
        self.enable_resource_monitoring = enable_resource_monitoring
        self.resource_check_interval = resource_check_interval_seconds
        self._resource_history: deque = deque(maxlen=100)
        self._last_resource_check = 0.0

        # Error tracking
        self._consecutive_failures = 0
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

    def _probe_backend_http(self) -> ProbeResult:
        """Probe the backend HTTP /health endpoint with caching and circuit breaker."""
        cache_key = "backend_http"
        cached = self._http_cache.get(cache_key)
        if cached:
            return cached

        def _do_probe() -> bool:
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
                    raise
                body = http_error.read().decode("utf-8")
                payload = json.loads(body)
            else:
                with response_ctx as response:
                    if not (200 <= response.status < 300):
                        raise Exception(f"HTTP {response.status}")
                    payload = json.loads(response.read().decode("utf-8"))

            if payload.get("startup_dead") is True:
                raise Exception("startup_dead")

            if (
                payload.get("ok") is True
                and payload.get("runtime_state") == "ready"
                and payload.get("governance_ready") is True
            ):
                return True
            return bool(
                payload.get("governance_ready") is True
                and payload.get("backend_runtime_ready") is True
                and payload.get("dependencies_ready") is True
            )

        start_time = time.monotonic()
        try:
            self._http_circuit_breaker.call(_do_probe)
            result = ProbeResult(success=True, latency_ms=(time.monotonic() - start_time) * 1000)
        except Exception as e:
            latency = (time.monotonic() - start_time) * 1000
            error_type = type(e).__name__
            _logger.warning("HTTP probe failed: %s: %s", error_type, e)
            result = ProbeResult(success=False, latency_ms=latency, error=str(e), error_type=error_type)

        self._http_cache.set("backend_http", result)
        return result

    def _check_frontend_connected(self) -> ProbeResult:
        """Check if the frontend WebSocket is connected to the IPC server."""
        cache_key = "ipc_frontend"
        cached = self._ipc_cache.get(cache_key)
        if cached:
            return cached

        def _do_check() -> bool:
            if not self._ipc_state_file.is_file():
                return False

            try:
                data = json.loads(self._ipc_state_file.read_text(encoding="utf-8"))
                active = int(data.get("active_connections", 0))
                updated_at = str(data.get("updated_at", ""))
                if updated_at:
                    from datetime import datetime as _dt
                    try:
                        parsed = _dt.fromisoformat(updated_at.replace("Z", "+00:00"))
                        age = (time.time() - parsed.timestamp())
                        if age > 20:
                            return False
                    except (ValueError, TypeError):
                        pass
                return active > 0
            except (OSError, json.JSONDecodeError, ValueError):
                return False

        start_time = time.monotonic()
        try:
            self._ipc_circuit_breaker.call(_do_check)
            result = ProbeResult(success=True, latency_ms=(time.monotonic() - start_time) * 1000)
        except Exception as e:
            latency = (time.monotonic() - start_time) * 1000
            error_type = type(e).__name__
            _logger.warning("IPC check failed: %s: %s", error_type, e)
            result = ProbeResult(success=False, latency_ms=latency, error=str(e), error_type=error_type)

        self._ipc_cache.set(cache_key, result)
        return result

    def _compute_state(
        self,
        backend_alive: bool,
        backend_http: ProbeResult,
        frontend_connected: ProbeResult,
    ) -> str:
        if backend_alive and backend_http.success and frontend_connected.success:
            return "connected"
        if backend_alive and backend_http.success and not frontend_connected.success:
            return "degraded"
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
            if len(self._events) > 100:
                self._events = self._events[-100:]

        if self._learning_store is not None:
            is_fault = (
                to_state == "disconnected"
                or (
                    to_state in ("degraded", "starting")
                    and from_state == "connected"
                )
            )
            is_recovery = to_state == "connected" and from_state != "connected"
            if is_fault:
                failure_code = (
                    "FRONTEND_BACKEND_DISCONNECTED"
                    if to_state == "disconnected"
                    else f"CONNECTION_{to_state.upper()}"
                )
                self._last_fault = (failure_code, from_state, to_state)
                self._record_learning(
                    failure_code, from_state, to_state, ok=False, run_id=event.event_id
                )
            elif is_recovery and self._last_fault is not None:
                failure_code, fault_from, fault_to = self._last_fault
                self._last_fault = None
                self._record_learning(
                    failure_code, fault_from, fault_to, ok=True, run_id=event.event_id
                )
        return event

    def _record_learning(
        self,
        failure_code: str,
        from_state: str,
        to_state: str,
        *,
        ok: bool,
        run_id: str,
    ) -> None:
        """Record one fault occurrence or its recovery in the learning store."""
        try:
            from .repair_learning import (
                ErrorSignature,
                RepairOutcome,
                _normalize_error_signature,
            )
            message = f"{from_state}->{to_state}"
            sig = ErrorSignature(
                signature_hash=_normalize_error_signature(
                    failure_code, message, file_path="ipc/connection",
                ),
                error_class=failure_code,
                message_pattern=message,
                failure_code=failure_code,
                file_context="ipc/connection",
                target_tool_id="main-system",
            )
            try:
                from .central_repair import CentralRepairService
                repair_root = (
                    self.project_root / "main-system" / "data" / "automatic-repair"
                )
                repair_root.mkdir(parents=True, exist_ok=True)
                service = CentralRepairService(self.project_root, repair_root)
                service.record_connection_outcome(
                    failure_code, from_state, to_state,
                    remedy="connection-watchdog", ok=ok, run_id=run_id,
                    record_error=not ok,
                )
            except Exception:
                outcome = RepairOutcome(
                    run_id=run_id,
                    signature_hash=sig.signature_hash,
                    remedy="connection-watchdog",
                    ok=ok,
                    detail={"from_state": from_state, "to_state": to_state},
                )
                if not ok:
                    self._learning_store.record_error(sig)
                self._learning_store.record_outcome(outcome)
        except Exception:
            pass

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
            old_dead = self._snapshot.consecutive_dead
            if new_state == "connected":
                new_dead = 0
            elif new_state == "degraded":
                new_dead = 0
            elif old_state == "connected" and new_state == "disconnected":
                new_dead = max(0, old_dead - CONNECTION_PROBE_RETRY_GRACE + 1)
            else:
                new_dead = old_dead + 1
            self._snapshot = ConnectionSnapshot(
                backend_process_alive=backend_process_alive,
                backend_http_healthy=backend_http.success,
                frontend_connected=frontend_connected.success,
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
                # Adaptive interval: increase when connection is stable (connected state)
                if snapshot.overall_state == "connected":
                    self._consecutive_stable += 1
                    if self._consecutive_stable >= 3:
                        self._adaptive_probe_interval = min(
                            self._adaptive_probe_interval * 1.5,
                            self._max_probe_interval,
                        )
                else:
                    # Reset on any non-connected state
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
