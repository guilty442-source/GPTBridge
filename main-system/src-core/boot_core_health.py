"""BootCore health monitoring mixin — health probing and health loop.

Provides HTTP health endpoint probing and the background health
monitoring thread for the BootCore supervisor.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request


class BootCoreHealthMixin:
    """Health monitoring methods for BootCore."""

    def _probe_health(self, port: int | None = None) -> bool:
        """Probe the backend HTTP /health endpoint.

        A67: a live socket alone is NOT "ready".  The backend is only healthy
        for boot_core purposes once the core runtime is ready:
        governance_ready=True, backend_runtime_ready=True, dependencies
        reachable, and startup_dead is not True.  The authenticated-IPC
        condition (frontend WebSocket) is a user-facing readiness concern,
        not a supervision-health concern — the supervisor must not kill a
        fully-started backend merely because the Electron app has not
        connected yet.

        The health endpoint returns HTTP 503 while the runtime is still
        starting (or when the frontend has not connected).  A 503 response
        still carries the full JSON payload, so we must read it rather than
        treating it as a connection failure.
        """
        try:
            probe_port = port or self._active_backend_port or self._health_probe_port
            request = urllib.request.Request(
                f"http://127.0.0.1:{probe_port}/health?brief=1",
                headers={"Connection": "close"},
            )
            try:
                response_ctx = self._http_opener.open(
                    request, timeout=self._health_probe_timeout
                )
            except urllib.error.HTTPError as http_error:
                # 503 STARTING is expected while the backend is coming up
                # or when the frontend has not connected.  Read the body
                # and evaluate the payload — do not treat it as a probe
                # failure.
                if http_error.code != 503:
                    return False
                body = http_error.read().decode("utf-8")
                payload = json.loads(body)
            else:
                with response_ctx as response:
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
            # session, not a dead generation.
            return bool(
                payload.get("governance_ready") is True
                and payload.get("backend_runtime_ready") is True
                and payload.get("dependencies_ready") is True
            )
        except (OSError, urllib.error.URLError, ValueError, UnicodeDecodeError):
            return False

    def _health_loop(self, epoch: int) -> None:
        """Background thread: periodically probe backend health for state file.

        The epoch guard binds this thread to one supervise generation: after
        a crash + respawn the outer loop increments ``_health_epoch`` and the
        stale thread exits instead of probing forever alongside its
        successor.  An in-generation handover keeps the same epoch, so the
        probe follows ``_active_backend_port`` to the standby port.
        """
        while not self._stop.is_set():
            if epoch != self._health_epoch:
                return
            if self._child is None or self._child.poll() is not None:
                break
            healthy = self._probe_health()
            if healthy and not self._probe_health(self._health_probe_port):
                # The backend generation can remain healthy while the stable
                # frontend gateway's accept loop or listener has failed.  In
                # that case repair only the gateway; never recycle or overwrite
                # the healthy backend generation.
                self._gateway.stop()
                try:
                    self._gateway.start()
                    if self._active_backend_port is not None:
                        self._gateway.activate(
                            self._active_backend_port, self._active_generation
                        )
                    healthy = self._probe_health(self._health_probe_port)
                except OSError as error:
                    healthy = False
                    self._last_exit = {
                        "error": f"gateway-recovery-failed: {type(error).__name__}: {error}"
                    }
            if healthy:
                self._unhealthy_since = None
            elif self._unhealthy_since is None:
                self._unhealthy_since = time.monotonic()
            if healthy != self._backend_healthy:
                self._backend_healthy = healthy
                # The restart budget counts consecutive failed generations,
                # not historical startup-gate failures.  Once a generation
                # reaches governed readiness it owns a fresh recovery budget.
                if healthy:
                    self._restarts = 0
                self._write_state(backend_healthy=healthy)
            interval = (
                self._health_probe_interval
                if self._backend_healthy
                else self._startup_health_probe_interval
            )
            if self._stop.wait(timeout=interval):
                break
