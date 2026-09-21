"""Connection watchdog — probe and audit mixins (A430 sub-module).

Extracted from ``connection_watchdog.py``: the probe methods
(``_probe_backend_http``, ``_check_frontend_connected``, ``_compute_state``)
and the audit methods (``_record_event``, ``_record_learning``,
``_write_state``) are composed into the ``ConnectionWatchdog`` class via
mixins to keep the class under the A430 300-effective-line limit.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any

from .connection_watchdog_types import (
    CONNECTION_PROBE_RETRY_GRACE,
    CONNECTION_WATCHDOG_VERSION,
    ConnectionEvent,
    ConnectionSnapshot,
    _iso_now,
)
from .connection_watchdog_probes import ProbeResult

_logger = logging.getLogger("gptbridge.connection_watchdog")


class ConnectionProbeMixin:
    """HTTP and IPC health probe methods (A430 sub-module)."""

    def _probe_backend_http(self) -> bool:
        """Probe the backend HTTP /health endpoint with caching and circuit breaker."""
        cache_key = "backend_http"
        cached = self._health_cache.get(cache_key)
        if cached:
            return cached.success

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
            ok = self._http_circuit_breaker.call(_do_probe)
            result = ProbeResult(success=bool(ok), latency_ms=(time.monotonic() - start_time) * 1000)
        except Exception as e:
            latency = (time.monotonic() - start_time) * 1000
            error_type = type(e).__name__
            _logger.warning("HTTP probe failed: %s: %s", error_type, e)
            result = ProbeResult(success=False, latency_ms=latency, error=str(e), error_type=error_type)
        self._health_cache.set("backend_http", result)
        return result.success

    def _check_frontend_connected(self) -> bool:
        """Check if the frontend WebSocket is connected to the IPC server."""
        cache_key = "ipc_frontend"
        cached = self._health_cache.get(cache_key)
        if cached:
            return cached.success

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
            ok = self._ipc_circuit_breaker.call(_do_check)
            result = ProbeResult(success=bool(ok), latency_ms=(time.monotonic() - start_time) * 1000)
        except Exception as e:
            latency = (time.monotonic() - start_time) * 1000
            error_type = type(e).__name__
            _logger.warning("IPC check failed: %s: %s", error_type, e)
            result = ProbeResult(success=False, latency_ms=latency, error=str(e), error_type=error_type)
        self._health_cache.set(cache_key, result)
        return result.success

    def _compute_state(
        self,
        backend_alive: bool,
        backend_http: bool,
        frontend_connected: bool,
    ) -> str:
        if backend_alive and backend_http and frontend_connected:
            return "connected"
        if backend_alive and backend_http and not frontend_connected:
            return "degraded"
        if not backend_alive:
            return "disconnected"
        return "starting"


class ConnectionAuditMixin:
    """Event recording, learning and state persistence (A430 sub-module)."""

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
            self._dispatch_learning(from_state, to_state, event)
        return event

    def _dispatch_learning(self, from_state: str, to_state: str, event: Any) -> None:
        """Classify a transition as fault or recovery and record it."""
        is_fault = (
            to_state == "disconnected"
            or (to_state in ("degraded", "starting") and from_state == "connected")
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
        elif is_recovery:
            if self._last_fault is not None:
                failure_code, fault_from, fault_to = self._last_fault
                self._last_fault = None
                self._record_learning(
                    failure_code, fault_from, fault_to, ok=True, run_id=event.event_id
                )
            self._absorb_recovered_connection_faults(run_id=event.event_id)

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
            self._record_via_central_repair(sig, failure_code, from_state, to_state, ok, run_id)
        except Exception:
            pass

    def _record_via_central_repair(
        self, sig: Any, failure_code: str, from_state: str,
        to_state: str, ok: bool, run_id: str,
    ) -> None:
        """Try CentralRepairService; fall back to direct learning-store recording."""
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
            from .repair_learning import RepairOutcome
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

    def _absorb_recovered_connection_faults(self, *, run_id: str) -> None:
        """Absorb outstanding watchdog failure evidence after recovery.

        A transition into ``connected`` proves every earlier
        watchdog-recorded fault resolved — including faults recorded by a
        previous watchdog generation, whose open fault never reaches this
        process's ``_last_fault``.  Each unabsorbed ``connection-watchdog``
        failure outcome gets a ``no-action-required`` reconciliation marker
        (``detail.source_outcome_id``): the evidence row is never deleted,
        it only leaves the live fault surface.  The marker carries
        ``ok=True`` under the absorbed signature so the learning store also
        sees the signature resolved.
        """
        store = self._learning_store
        if store is None:
            return
        try:
            from uuid import uuid4

            from .repair_learning import (
                NON_ACTIONABLE_REMEDY,
                RECONCILIATION_SOURCE_FIELD,
                RepairOutcome,
                absorbed_outcome_ids,
            )

            connection = store._connect()
            try:
                absorbed = absorbed_outcome_ids(connection)
                rows = connection.execute(
                    "SELECT outcome_id, signature_hash, detail_json "
                    "FROM repair_outcomes WHERE ok = 0 AND remedy = ? "
                    "ORDER BY recorded_at DESC LIMIT 200",
                    ("connection-watchdog",),
                ).fetchall()
            finally:
                connection.close()
            for outcome_id, signature_hash, detail_json in rows:
                if str(outcome_id) in absorbed:
                    continue
                detail: dict[str, Any] = {}
                try:
                    parsed = json.loads(detail_json or "{}")
                    if isinstance(parsed, dict):
                        detail = parsed
                except (TypeError, ValueError):
                    detail = {}
                store.record_outcome(
                    RepairOutcome(
                        run_id=run_id or uuid4().hex,
                        signature_hash=str(signature_hash or ""),
                        remedy=NON_ACTIONABLE_REMEDY,
                        ok=True,
                        detail={
                            RECONCILIATION_SOURCE_FIELD: str(outcome_id),
                            "reason": "connection-recovered",
                            "from_state": str(detail.get("from_state") or ""),
                            "to_state": str(detail.get("to_state") or ""),
                        },
                    )
                )
        except Exception:
            pass  # Reconciliation is best-effort; never break probing.

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


__all__ = ["ConnectionAuditMixin", "ConnectionProbeMixin"]
