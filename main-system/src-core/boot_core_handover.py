"""BootCore generation handover mixin — hot-update handover and drain.

Provides certified hot-update generation handover, connection draining,
and stability verification for the BootCore supervisor.
"""

from __future__ import annotations

import json
import os
import threading
import time
import subprocess
from typing import Any

from boot_core_state import _iso_now


class BootCoreHandoverMixin:
    """Generation handover methods for BootCore."""

    def _read_update_request(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self._update_request_path.read_text("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        operation_id = str(payload.get("operation_id") or "")
        # The requester writes "prepared" for a pending handover; boot_core
        # marks a terminal status (global-success / failed-isolated /
        # rolled-back / partial-deferred) into the same file once the
        # operation converges.  A request that already reached a terminal
        # state must not spawn another standby generation on every boot —
        # retries belong to a fresh certified request, not the converged one.
        status = str(payload.get("terminal_status") or "")
        if (
            not operation_id
            or operation_id == self._last_update_operation
            or payload.get("certified") is not True
            or not payload.get("artifact_hashes")
            or status not in ("", "prepared")
        ):
            return None
        return payload

    def _mark_update_request(self, payload: dict[str, Any], **result: Any) -> None:
        recorded = {**payload, **result, "processed_at": _iso_now()}
        try:
            temporary = self._update_request_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(recorded, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._update_request_path)
        except OSError:
            pass

    def _maybe_handover(
        self, args: list[str], startup_state: str
    ) -> bool:
        """Prepare a standby generation and atomically route new sessions to it.

        A330 convergence: the handover is only marked global-success after
        (1) the standby accepts new connections, (2) old connections drain
        within a bounded window, and (3) the new generation remains healthy
        for a stability window.  If the new generation fails within the
        rollback window, the gateway switches back to the old generation
        and the handover is marked rolled-back.
        """
        request = self._read_update_request()
        if request is None or self._child is None:
            return False
        operation_id = str(request["operation_id"])
        self._last_update_operation = operation_id
        prepared = self._prepare_standby(request, args, startup_state)
        if prepared is None:
            return False
        standby, standby_port, generation = prepared

        old_child = self._child
        old_port, old_generation = self._activate_standby(
            standby, standby_port, generation, operation_id
        )

        # A330 convergence: drain old connections, then verify the new
        # generation remains healthy for a stability window before marking
        # global-success.  If the new generation fails within the rollback
        # window, switch the gateway back to the old generation.
        converged = self._drain_and_verify(
            old_port=old_port,
            new_port=standby_port,
            standby=standby,
        )
        if not converged:
            self._rollback_handover(
                request, standby, old_child, old_port, old_generation, operation_id
            )
            return False

        self._mark_update_request(
            request,
            terminal_status="global-success",
            active_generation=generation,
            active_backend_port=standby_port,
        )

        # New connections already use the standby. Give old WebSocket sessions
        # a bounded drain, then close them so frontend generation fencing causes
        # an authenticated snapshot/replay reconnect to the new backend.
        threading.Thread(
            target=self._drain_old_generation,
            args=(old_port, old_child),
            name="backend-generation-drain",
            daemon=True,
        ).start()
        return True

    def _prepare_standby(
        self, request: dict, args: list[str], startup_state: str
    ) -> tuple | None:
        operation_id = str(request["operation_id"])
        active_port = self._active_backend_port
        if active_port is None:
            self._mark_update_request(
                request,
                terminal_status="failed-isolated",
                error="no-active-backend-port",
            )
            return None
        standby_port = next(
            port for port in self._backend_generation_ports if port != active_port
        )
        generation = str(request.get("target_generation") or operation_id)
        if not self._wait_port_available(standby_port):
            self._mark_update_request(
                request,
                terminal_status="failed-isolated",
                error="standby-port-not-released",
            )
            return None
        try:
            # A standby generation owns a fresh startup window: re-anchor
            # the deadline epoch to this spawn, not the supervise cycle
            # that produced the first generation.  The startup_dead latch
            # belongs to the previous generation — reset it so the
            # standby's readiness wait is judged on its own health.
            self._boot_epoch_wall = time.time()
            self._backend_startup_dead = False
            standby = self._spawn_backend(
                args,
                startup_state=startup_state,
                generation_id=generation,
                backend_port=standby_port,
            )
        except OSError as error:
            self._mark_update_request(
                request,
                terminal_status="failed-isolated",
                error=f"spawn: {type(error).__name__}: {error}",
            )
            return None
        if standby.stdout is not None:
            threading.Thread(
                target=self._relay, args=(standby.stdout,), daemon=True
            ).start()
        if not self._wait_backend_ready(standby, standby_port, 45.0):
            self._terminate_process(standby)
            self._mark_update_request(
                request,
                terminal_status="failed-isolated",
                error="standby-readiness-failed",
            )
            return None
        return standby, standby_port, generation

    def _activate_standby(
        self, standby, standby_port: int, generation: str, operation_id: str
    ) -> tuple:
        old_port = self._active_backend_port
        old_generation = self._active_generation
        self._gateway.activate(standby_port, generation)
        self._child = standby
        self._active_backend_port = standby_port
        self._active_generation = generation
        self._backend_healthy = True
        self._unhealthy_since = None
        self._status = "backend-running"
        self._write_state(
            backend_healthy=True,
            active_generation=generation,
            active_backend_port=standby_port,
            gateway_port=self._health_probe_port,
            previous_generation_port=old_port,
            update_operation_id=operation_id,
        )
        return old_port, old_generation

    def _rollback_handover(
        self,
        request: dict,
        standby,
        old_child,
        old_port: int,
        old_generation: str,
        operation_id: str,
    ) -> None:
        # Rollback: switch gateway back to the old generation.
        self._gateway.activate(old_port, old_generation)
        self._terminate_process(standby)
        self._child = old_child
        self._active_backend_port = old_port
        self._active_generation = old_generation
        self._write_state(
            backend_healthy=True,
            active_generation=old_generation,
            active_backend_port=old_port,
            gateway_port=self._health_probe_port,
            update_operation_id=operation_id,
            rollback=True,
        )
        self._mark_update_request(
            request,
            terminal_status="rolled-back",
            error="standby-unhealthy-after-activation",
            active_generation=old_generation,
            active_backend_port=old_port,
        )

    def _drain_old_generation(self, old_port: int, old_child) -> None:
        deadline = time.monotonic() + 5.0
        while (
            time.monotonic() < deadline
            and self._gateway.connection_count(old_port) > 0
            and not self._stop.is_set()
        ):
            self._stop.wait(0.1)
        self._gateway.close_generation_connections(old_port)
        self._terminate_process(old_child)

    def _drain_and_verify(
        self,
        *,
        old_port: int,
        new_port: int,
        standby: subprocess.Popen[bytes],
        drain_timeout: float = 5.0,
        stability_window: float = 3.0,
    ) -> bool:
        """A330 convergence: drain old connections and verify the new
        generation remains healthy for a stability window.

        Returns True only if:
        1. Old connections drain within ``drain_timeout`` (or are force-closed)
        2. The new generation is HTTP-healthy immediately after activation
        3. The new generation remains healthy for ``stability_window`` seconds
        4. The standby process is still alive throughout
        """
        # 1. Drain old connections (bounded).
        drain_deadline = time.monotonic() + drain_timeout
        while (
            time.monotonic() < drain_deadline
            and self._gateway.connection_count(old_port) > 0
            and not self._stop.is_set()
        ):
            self._stop.wait(0.1)

        # 2. Verify standby is alive and healthy immediately after activation.
        if standby.poll() is not None:
            return False
        if not self._probe_health(new_port):
            return False

        # 3. Stability window: remain healthy for ``stability_window`` seconds.
        stability_deadline = time.monotonic() + stability_window
        while time.monotonic() < stability_deadline and not self._stop.is_set():
            if standby.poll() is not None:
                return False
            if not self._probe_health(new_port):
                return False
            self._stop.wait(0.5)

        return True
