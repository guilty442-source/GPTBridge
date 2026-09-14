"""Startup-sovereign source runtime and main-backend supervisor — facade.

This module combines the BootCore mixins and provides the main
supervise loop.  Implementation details live in submodules:

  * :mod:`boot_core_state` — state, paths, governance bootstrap.
  * :mod:`boot_core_health` — health probing and health loop.
  * :mod:`boot_core_lifecycle` — spawn, relay, terminate, readiness.
  * :mod:`boot_core_handover` — generation handover and drain.
  * :mod:`boot_core_repair` — connection watchdog, auto-repair, gateway.

Entry responsibility boundary (per architecture decision A192/A193):

  * 啟動入口 (Electron main) — only wakes the screen; it spawns this
    startup core and does not manage the backend directly.
  * This process (boot_core) hosts the startup sovereign CAPABILITY 1:
    bootstrap-and-authority-readiness-orchestration (phases 0-5).
  * CAPABILITY 2 (startup_executor in main.py) owns phase 6 + readiness handoff.
  * After phase 6 completes, boot_core spawns main.py --serve with
    GPTBRIDGE_STARTUP_STATE=READY and the generation ID.
  * Crash recovery — when the backend crashes with a non-zero exit code
    during startup (uptime < 30s), the startup core diagnoses the crash
    traceback and writes a repair signal to the information layer.
  * Independent tool isolation — independent tools are spawned in their
    own process group so they survive a main-system crash or restart.
  * Single-fault isolation — a backend crash is restarted here with bounded
    backoff; if THIS process dies, the launcher's own recovery respawns it.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

from startup_core.phases import PhaseMixin
from startup_core.governance import GovernanceMixin
from startup_core.startup_config import port as _cfg_port, supervisor_constant as _cfg_supervisor
from tasks.crash_diagnosis import CrashDiagnoser
from backend_gateway import BackendGateway

from boot_core_state import BootCoreStateMixin, _iso_now
from boot_core_health import BootCoreHealthMixin
from boot_core_lifecycle import BootCoreLifecycleMixin
from boot_core_handover import BootCoreHandoverMixin
from boot_core_repair import BootCoreRepairMixin

MAX_RESTARTS = _cfg_supervisor("max_restarts")
BACKOFF_SCHEDULE_SECONDS = _cfg_supervisor("backoff_schedule_seconds")
HEALTHY_UPTIME_RESET_SECONDS = _cfg_supervisor("healthy_uptime_reset_seconds")
HEALTH_PROBE_PORT = _cfg_port("health_probe")
BACKEND_GENERATION_PORTS = (HEALTH_PROBE_PORT + 1, HEALTH_PROBE_PORT + 2)
HEALTH_PROBE_TIMEOUT = _cfg_supervisor("health_probe_timeout")
HEALTH_PROBE_INTERVAL = _cfg_supervisor("health_probe_interval")
STARTUP_HEALTH_PROBE_INTERVAL = _cfg_supervisor("startup_health_probe_interval")
STATE_RELATIVE = ("main-system", "runtime", "state", "boot-core.json")
CRASH_REPAIR_UPTIME_THRESHOLD = _cfg_supervisor("crash_repair_uptime_threshold")


class BootCore(
    BootCoreStateMixin,
    BootCoreHealthMixin,
    BootCoreLifecycleMixin,
    BootCoreHandoverMixin,
    BootCoreRepairMixin,
    PhaseMixin,
    GovernanceMixin,
):
    """Spawn, relay, and supervise the main backend process."""

    def __init__(self) -> None:
        src_core = Path(__file__).resolve().parent
        self.project_root = src_core.parents[1]
        self.workspace_root = src_core.parents[1]
        self.backend_entry = src_core / "main.py"
        self.state_path = self.project_root.joinpath(*STATE_RELATIVE)
        self._stop = threading.Event()
        self._child: subprocess.Popen[bytes] | None = None
        self._active_backend_port: int | None = None
        self._active_generation = ""
        self._gateway = BackendGateway(HEALTH_PROBE_PORT)
        self._update_request_path = self.project_root / "main-system" / "runtime" / "state" / "backend-update-request.json"
        self._last_update_operation = ""
        self._restarts = 0
        self._last_exit: dict[str, object] = {}
        self._status = "starting"
        self._backend_healthy = False
        self._unhealthy_since: float | None = time.monotonic()
        self._health_epoch = 0
        self._health_thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._connection_watchdog: Any = None
        self._child_output: list[str] = []
        self._child_output_lock = threading.Lock()
        self._http_opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({})
        )
        self._crash_diagnoser = CrashDiagnoser()
        # Shared config constants for mixin access
        self._max_restarts = MAX_RESTARTS
        self._health_probe_port = HEALTH_PROBE_PORT
        self._health_probe_timeout = HEALTH_PROBE_TIMEOUT
        self._health_probe_interval = HEALTH_PROBE_INTERVAL
        self._startup_health_probe_interval = STARTUP_HEALTH_PROBE_INTERVAL
        self._backend_generation_ports = BACKEND_GENERATION_PORTS
        self._crash_repair_uptime_threshold = CRASH_REPAIR_UPTIME_THRESHOLD

    def _install_signals(self) -> None:
        def _stop_handler(_signum: int, _frame: object) -> None:
            self._stop.set()

        for sig_name in ("SIGINT", "SIGTERM"):
            sig = getattr(signal, sig_name, None)
            if sig is not None:
                try:
                    signal.signal(sig, _stop_handler)
                except (OSError, ValueError):
                    pass

    # --------------------------------------------------------------
    # supervise loop (split into sub-methods for A430 function limit)
    # --------------------------------------------------------------

    def run(self, args: list[str]) -> int:
        self._install_signals()
        try:
            self._start_gateway("--auto-kill-backend-port" in args)
        except OSError as error:
            self._status = "gateway-bind-failed"
            self._last_exit = {"error": f"{type(error).__name__}: {error}"}
            self._write_state()
            return 4
        self._write_state()
        while not self._stop.is_set():
            result = self._run_supervise_cycle(args)
            if result is not None:
                return result
        self._stop_connection_watchdog()
        self._gateway.stop()
        self._status = "stopped"
        self._write_state()
        return 0

    def _run_supervise_cycle(self, args: list[str]) -> int | None:
        """Run one supervise cycle. Returns exit code or None to continue."""
        # --- five pre-spawn dependency gates (phases 0-5) ---
        startup = self._run_startup_phases()
        self._write_orchestrator_report(startup)

        if not startup.get("gate_ok"):
            return self._handle_phase_failure(args, startup)

        # --- all required pre-spawn gates verified — CAPABILITY 2 takes over ---
        return self._spawn_and_supervise(args, startup)

    def _handle_phase_failure(self, args: list[str], startup: dict[str, Any]) -> int | None:
        """Handle a failed startup phase gate."""
        self._status = "startup-phase-blocked"
        self._last_exit = {
            "startup": "required-phase-failed",
            "startup_state": startup.get("state", "FAILED"),
            "at": _iso_now(),
        }
        self._write_state()
        self._signal_startup_failure(1, 0.0)
        if self._restarts >= MAX_RESTARTS:
            self._gateway.stop()
            return 2
        self._restarts += 1
        delay = BACKOFF_SCHEDULE_SECONDS[
            min(self._restarts - 1, len(BACKOFF_SCHEDULE_SECONDS) - 1)
        ]
        self._status = "backend-restarting"
        self._write_state(next_retry_in_seconds=delay)
        if self._stop.wait(timeout=delay):
            self._status = "stopped"
            self._write_state()
            self._gateway.stop()
            return 0
        return None

    def _spawn_and_supervise(self, args: list[str], startup: dict[str, Any]) -> int | None:
        """Spawn backend and supervise until exit. Returns exit code or None."""
        generation_id = startup.get("generation_id", "")
        startup_state = startup.get("state", "")
        self._status = "handoff-to-capability-2"
        self._write_state(startup_state=startup_state, generation_id=generation_id)

        try:
            self._active_backend_port = BACKEND_GENERATION_PORTS[0]
            self._active_generation = str(generation_id)
            self._child = self._spawn_backend(
                args,
                startup_state=startup_state,
                generation_id=generation_id,
                backend_port=self._active_backend_port,
            )
        except OSError as error:
            self._status = "spawn-failed"
            self._last_exit = {"error": f"{type(error).__name__}: {error}"}
            self._write_state()
            self._signal_startup_failure(1, 0.0)
            if self._restarts >= MAX_RESTARTS:
                self._gateway.stop()
                return 2
            self._restarts += 1
            delay = BACKOFF_SCHEDULE_SECONDS[
                min(self._restarts - 1, len(BACKOFF_SCHEDULE_SECONDS) - 1)
            ]
            self._status = "backend-restarting"
            self._write_state(next_retry_in_seconds=delay)
            if self._stop.wait(timeout=delay):
                self._status = "stopped"
                self._write_state()
                self._gateway.stop()
                return 0
            return None

        child_started_at = time.monotonic()
        self._backend_healthy = False
        self._unhealthy_since = time.monotonic()
        self._status = "backend-running"
        self._gateway.activate(self._active_backend_port, self._active_generation)
        self._write_state(
            gateway_port=HEALTH_PROBE_PORT,
            active_backend_port=self._active_backend_port,
            active_generation=self._active_generation,
        )
        relay = threading.Thread(
            target=self._relay, args=(self._child.stdout,), daemon=True
        )
        relay.start()
        self._health_epoch += 1
        self._health_thread = threading.Thread(
            target=self._health_loop, args=(self._health_epoch,), daemon=True
        )
        self._health_thread.start()
        self._start_connection_watchdog()
        dead_grace_seconds = float(
            _cfg_supervisor("startup_dead_grace_seconds") or 45.0
        )
        dead_generation = self._monitor_backend(args, startup_state, dead_grace_seconds)
        if self._stop.is_set():
            self._terminate_child()
            self._stop_connection_watchdog()
            self._status = "stopped"
            self._write_state()
            self._gateway.stop()
            return 0
        return self._handle_backend_exit(child_started_at, dead_generation)

    def _monitor_backend(
        self, args: list[str], startup_state: str, dead_grace_seconds: float
    ) -> bool:
        """Monitor the running backend until it exits or dies. Returns dead_generation."""
        dead_generation = False
        while not self._stop.is_set():
            code = self._child.poll()
            if code is not None:
                break
            if self._maybe_handover(args, startup_state):
                dead_generation = False
                continue
            if (
                not self._backend_healthy
                and self._unhealthy_since is not None
                and time.monotonic() - self._unhealthy_since
                > dead_grace_seconds
            ):
                dead_generation = True
                self._terminate_child()
                break
            time.sleep(0.5)
        return dead_generation

    def _handle_backend_exit(self, child_started_at: float, dead_generation: bool) -> int | None:
        """Handle backend process exit. Returns exit code or None to continue."""
        code = int(self._child.returncode or 0)
        self._last_exit = {
            "code": code,
            "at": _iso_now(),
            **(
                {"reason": "startup-generation-dead-grace-exceeded"}
                if dead_generation
                else {}
            ),
        }
        if code == 0:
            self._stop_connection_watchdog()
            self._status = "backend-stopped-clean"
            self._write_state()
            self._gateway.stop()
            return 0
        healthy_uptime = time.monotonic() - child_started_at
        if healthy_uptime >= HEALTHY_UPTIME_RESET_SECONDS:
            self._restarts = 0
        self._restarts += 1
        if self._restarts > MAX_RESTARTS:
            self._stop_connection_watchdog()
            self._status = "restart-budget-exhausted"
            self._write_state()
            self._gateway.stop()
            return 3
        repair_report = self._signal_startup_failure(code, healthy_uptime)
        delay = BACKOFF_SCHEDULE_SECONDS[
            min(self._restarts - 1, len(BACKOFF_SCHEDULE_SECONDS) - 1)
        ]
        self._status = "backend-restarting"
        self._write_state(next_retry_in_seconds=delay, repair=repair_report)
        if self._stop.wait(timeout=delay):
            self._status = "stopped"
            self._write_state()
            self._gateway.stop()
            return 0
        return None


def main() -> int:
    return BootCore().run([*sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
