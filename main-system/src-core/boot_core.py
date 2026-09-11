"""Startup-sovereign source runtime and main-backend supervisor.

Entry responsibility boundary (per architecture decision):

  * 啟動入口 (Electron main) — only wakes the screen; it spawns this
    startup core and does not manage the backend directly.
  * This process hosts the startup sovereign. It validates bootstrap
    readiness, activates the certified dependency DAG, and hands verified
    readiness to the system-runtime sovereign.
    It then spawns the main backend (``main.py --serve``).  After spawning,
    boot_core waits for the backend's own ``governance-system-start`` to
    complete by probing ``/health`` until ``governance_ready`` is true
    (A67 — a live socket alone is not ready).  Only then is the backend
    considered healthy and supervised for its whole lifetime.
  * Crash recovery — when the backend crashes with a non-zero exit code
    during startup (uptime < 30s), the startup core diagnoses the crash
    traceback and writes a repair signal to the information layer
    (``repair-requests.json``) via ``RepairCoordinator`` (A72:
    signal-and-request-only).  The maintenance sovereign's repair
    decision chain (A67/A72) picks up the signal, makes the repair
    decision, and dispatches the governed executor.  The boot core
    never executes repair mutations itself.
  * Independent tool isolation — independent tools (非常駐服務) are spawned
    in their own process group (CREATE_NEW_PROCESS_GROUP) so they survive a
    main-system crash or restart.  The startup core only terminates the
    main backend process itself, never the independent tool processes.
  * Single-fault isolation — a backend crash is restarted here with bounded
    backoff; if THIS process dies, the launcher's own recovery respawns it
    while the backend (if still alive) keeps serving.

The startup source runtime:
  1. Runs bootstrap gates and the contract-declared dependency DAG.
  2. Generates the governance bootstrap token in-process.
  3. Writes the orchestrator report to ``launcher/state/orchestrator-report.json``
     for system_sovereign consumption (stale-safe: always overwritten on boot).
  4. Sets ``GPTBRIDGE_STARTUP_STATE`` in the child env (READY/DEGRADED/FAILED).
  5. Spawns main.py, relays stdout/stderr, and supervises the child lifetime.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from startup_core.phases import PhaseMixin
from startup_core.governance import GovernanceMixin
from startup_core.startup_config import port as _cfg_port, supervisor_constant as _cfg_supervisor
from tasks.crash_diagnosis import CrashDiagnoser

MAX_RESTARTS = _cfg_supervisor("max_restarts")
BACKOFF_SCHEDULE_SECONDS = _cfg_supervisor("backoff_schedule_seconds")
HEALTHY_UPTIME_RESET_SECONDS = _cfg_supervisor("healthy_uptime_reset_seconds")
HEALTH_PROBE_PORT = _cfg_port("health_probe")
HEALTH_PROBE_TIMEOUT = _cfg_supervisor("health_probe_timeout")
HEALTH_PROBE_INTERVAL = _cfg_supervisor("health_probe_interval")
# Fast cadence until the first healthy probe — the supervised interval is
# for steady-state monitoring; startup readiness detection must not wait
# up to HEALTH_PROBE_INTERVAL before noticing the backend came up.
STARTUP_HEALTH_PROBE_INTERVAL = _cfg_supervisor("startup_health_probe_interval")
STATE_RELATIVE = ("main-system", "runtime", "state", "boot-core.json")

# Early crashes that trigger diagnosis and a governed repair request.
CRASH_REPAIR_UPTIME_THRESHOLD = _cfg_supervisor("crash_repair_uptime_threshold")


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BootCore(PhaseMixin, GovernanceMixin):
    """Spawn, relay, and supervise the main backend process."""

    def __init__(self) -> None:
        src_core = Path(__file__).resolve().parent
        self.project_root = src_core.parents[1]
        self.workspace_root = src_core.parents[1]
        self.backend_entry = src_core / "main.py"
        self.state_path = self.project_root.joinpath(*STATE_RELATIVE)
        self._stop = threading.Event()
        self._child: subprocess.Popen[bytes] | None = None
        self._restarts = 0
        self._last_exit: dict[str, object] = {}
        self._status = "starting"
        self._backend_healthy = False
        self._health_thread: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        self._connection_watchdog: Any = None
        # Rolling buffer of recent child stdout lines for crash diagnosis.
        self._child_output: list[str] = []
        self._child_output_lock = threading.Lock()
        # Startup authority is diagnosis-and-signal only. Repair mutation is
        # owned by the governed maintenance/decision/execution chain.
        self._crash_diagnoser = CrashDiagnoser()

    # --------------------------------------------------------------
    # runtime paths (shared by governance bootstrap + phase imports)
    # --------------------------------------------------------------

    def _ensure_runtime_paths(self) -> None:
        workspace = str(self.workspace_root)
        src_core = str(self.workspace_root / "main-system" / "src-core")
        shared = str(self.workspace_root / "shared-layer" / "src")
        for p in (workspace, src_core, shared):
            if p not in sys.path:
                sys.path.insert(0, p)

    # --------------------------------------------------------------
    # governance bootstrap
    # --------------------------------------------------------------


    # --------------------------------------------------------------
    # state
    # --------------------------------------------------------------

    def _write_state(self, **extra: object) -> None:
        payload = {
            "role": "boot-core",
            "status": self._status,
            "pid": os.getpid(),
            "backend_pid": self._child.pid if self._child is not None else None,
            "restarts": self._restarts,
            "max_restarts": MAX_RESTARTS,
            "last_exit": self._last_exit,
            "updated_at": _iso_now(),
            **extra,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self.state_path)
        except OSError:
            pass

    # --------------------------------------------------------------
    # six-phase startup orchestrator (A61/E47/P26)
    # --------------------------------------------------------------




    def _write_orchestrator_report(self, report: dict[str, Any]) -> None:
        """Write orchestrator report for system_sovereign consumption."""
        path = (
            self.workspace_root
            / "main-system"
            / "launcher"
            / "state"
            / "orchestrator-report.json"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(tmp, path)
        except OSError:
            pass

    # --------------------------------------------------------------
    # child lifecycle
    # --------------------------------------------------------------

    def _python_executable(self) -> str:
        exe = Path(sys.executable).resolve()
        if os.name == "nt":
            pythonw = exe.with_name("pythonw.exe")
            if pythonw.is_file():
                return os.fspath(pythonw)
        return os.fspath(exe)

    def _spawn_backend(
        self, args: list[str], startup_state: str = ""
    ) -> subprocess.Popen[bytes]:
        command = [
            self._python_executable(),
            "-u",
            "-B",
            os.fspath(self.backend_entry),
            *args,
        ]
        creationflags = (
            int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
            | int(getattr(subprocess, "DETACHED_PROCESS", 0) or 0)
        )
        env = dict(os.environ)
        # Generate a fresh governance bootstrap token for each spawn so the
        # 30-second identity attestation expiry is always within window.
        try:
            # Bootstrap attestations are single-use. Never inherit a token
            # consumed by the previous backend process.
            env["GPTBRIDGE_GOVERNANCE_BOOTSTRAP"] = (
                self._generate_governance_bootstrap()
            )
        except Exception as error:
            self._last_exit = {
                "error": f"governance-bootstrap-failed: {type(error).__name__}: {error}"
            }
            self._status = "governance-bootstrap-failed"
            self._write_state()
            raise
        env["GPTBRIDGE_PROJECT_ROOT"] = str(self.workspace_root)
        if startup_state:
            env["GPTBRIDGE_STARTUP_STATE"] = startup_state
        return subprocess.Popen(  # noqa: S603 - governed local spawn
            command,
            cwd=os.fspath(self.project_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

    def _probe_health(self) -> bool:
        """Probe the backend HTTP /health endpoint.

        A67: a live socket alone is NOT "ready".  The backend is only healthy
        for boot_core purposes once ok=True, runtime_state=ready,
        governance_ready=True and startup_dead is not True.
        """
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{HEALTH_PROBE_PORT}/health?brief=1",
                headers={"Connection": "close"},
            )
            with urllib.request.urlopen(
                request, timeout=HEALTH_PROBE_TIMEOUT
            ) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return bool(
                    payload.get("ok") is True
                    and payload.get("runtime_state") == "ready"
                    and payload.get("governance_ready") is True
                    and payload.get("startup_dead") is not True
                )
        except (OSError, urllib.error.URLError, ValueError, UnicodeDecodeError):
            return False

    def _health_loop(self) -> None:
        """Background thread: periodically probe backend health for state file."""
        while not self._stop.is_set():
            if self._child is None or self._child.poll() is not None:
                break
            healthy = self._probe_health()
            if healthy != self._backend_healthy:
                self._backend_healthy = healthy
                # The restart budget counts consecutive failed generations,
                # not historical startup-gate failures.  Once a generation
                # reaches governed readiness it owns a fresh recovery budget.
                if healthy:
                    self._restarts = 0
                self._write_state(backend_healthy=healthy)
            interval = (
                HEALTH_PROBE_INTERVAL
                if self._backend_healthy
                else STARTUP_HEALTH_PROBE_INTERVAL
            )
            if self._stop.wait(timeout=interval):
                break

    def _start_connection_watchdog(self) -> None:
        """Start the connection watchdog thread."""
        try:
            self._ensure_runtime_paths()
            from tasks.connection_watchdog import ConnectionWatchdog
            from tasks.repair_learning import RepairLearningStore

            repair_data = self.project_root / "main-system" / "data" / "automatic-repair"
            repair_data.mkdir(parents=True, exist_ok=True)
            learning_store = RepairLearningStore(repair_data)

            cwd = ConnectionWatchdog(
                self.project_root,
                health_port=HEALTH_PROBE_PORT,
            )
            cwd.set_learning_store(learning_store)
            cwd.set_repair_callback(self._on_connection_disconnected)
            self._connection_watchdog = cwd

            def backend_alive() -> bool:
                return self._child is not None and self._child.poll() is None

            self._watchdog = threading.Thread(
                target=cwd.run, args=(backend_alive,), daemon=True,
                name="connection-watchdog",
            )
            self._watchdog.start()
        except Exception:
            pass  # Watchdog is best-effort; never block boot_core.

    def _stop_connection_watchdog(self) -> None:
        """Stop the connection watchdog thread."""
        if self._connection_watchdog is not None:
            try:
                self._connection_watchdog.stop()
            except Exception:
                pass
        if self._watchdog is not None and self._watchdog.is_alive():
            self._watchdog.join(timeout=2.0)
        self._connection_watchdog = None
        self._watchdog = None

    def _on_connection_disconnected(self, failure_code: str, snapshot: Any) -> None:
        """Callback when the connection watchdog detects a persistent disconnection.

        A67 failure path: maintenance-sovereign-decides > sub-sovereign-dispatch
        > governed-executor-repairs > boot-core-revalidates > ui-resynchronizes.
        A67 FORBID:duplicate-repair-owner — acquire the repair coordination
        lock before acting; if another owner already holds it, do not
        duplicate the repair.
        """
        try:
            self._ensure_runtime_paths()
            from tasks.repair_coordinator import RepairCoordinator

            # Cross-process coordination via the shared state file.
            coordinator = RepairCoordinator(self.project_root)
            if not coordinator.try_acquire(
                failure_code=failure_code,
                owner="boot-core-connection-watchdog",
            ):
                # Another owner is already repairing — do not duplicate.
                return

            from tasks.central_repair import CentralRepairService

            repair_root = self.project_root / "main-system" / "data" / "automatic-repair"
            repair_root.mkdir(parents=True, exist_ok=True)
            service = CentralRepairService(self.project_root, repair_root)
            # Consult learned recipes with the consistent connection signature
            # (continuous learning: recorded outcomes are discoverable here).
            try:
                suggestion = service.suggest_connection_remedy(
                    failure_code,
                    getattr(snapshot, "overall_state", "unknown"),
                    "disconnected",
                )
                if suggestion.get("suggested"):
                    # A learned recipe exists — record that it was consulted.
                    service.record_connection_outcome(
                        failure_code,
                        getattr(snapshot, "overall_state", "unknown"),
                        "disconnected",
                        remedy=str(suggestion.get("remedy", "connection-watchdog")),
                        ok=False,
                        run_id=f"watchdog-{int(time.time())}",
                    )
            except Exception:
                pass
            # Record the connection failure for learning.
            service.record_connection_outcome(
                failure_code,
                getattr(snapshot, "overall_state", "unknown"),
                "disconnected",
                remedy="connection-watchdog",
                ok=False,
                run_id=f"watchdog-{int(time.time())}",
            )
            # Release the lock so the frontend or boot_core restart can proceed.
            coordinator.release(
                owner="boot-core-connection-watchdog",
                failure_code=failure_code,
            )
        except Exception:
            pass  # Learning is best-effort.

    def _relay(self, stream: object) -> None:
        """Forward child output so the launcher sees readiness lines.

        Also captures the last N lines into ``_child_output`` so that
        ``_run_auto_repair`` can diagnose the actual crash cause instead
        of running a blind full-source scan.
        """

        try:
            for raw in iter(stream.readline, b""):
                try:
                    sys.stdout.buffer.write(raw)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError):
                    return
                try:
                    line = raw.decode("utf-8", errors="replace").rstrip("\n\r")
                    with self._child_output_lock:
                        self._child_output.append(line)
                        # Keep only the last 200 lines — enough for any
                        # realistic traceback without unbounded memory.
                        if len(self._child_output) > 200:
                            del self._child_output[:100]
                except Exception:
                    pass
        except (ValueError, OSError):
            return

    def _terminate_child(self) -> None:
        child = self._child
        if child is None or child.poll() is not None:
            return
        try:
            child.terminate()
            child.wait(timeout=10)
        except Exception:
            try:
                child.kill()
            except Exception:
                pass

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
    # Automatic repair on crash
    # --------------------------------------------------------------

    def _signal_startup_failure(
        self, exit_code: int, uptime: float
    ) -> dict[str, object]:
        """Diagnose an early crash and submit a governed repair signal.

        This startup-owned path performs no repair mutation and makes no
        maintenance decision.
        """

        report: dict[str, object] = {
            "triggered": False,
            "reason": "",
            "ok": False,
        }
        # Development crashes are surfaced to the active developer and do not
        # create background repair requests.
        if os.environ.get("GPTBRIDGE_RENDERER_DEV_URL"):
            report["reason"] = "dev-mode; auto-repair disabled"
            return report

        # Only repair on crashes that happened early (likely startup failure
        # from corrupted source) and with a non-zero exit code.
        if exit_code == 0 or uptime >= CRASH_REPAIR_UPTIME_THRESHOLD:
            report["reason"] = "crash-after-stable-uptime; repair not indicated"
            return report

        report["triggered"] = True
        report["reason"] = f"crash-exit-code-{exit_code}-uptime-{uptime:.1f}s"
        self._status = "auto-repairing"
        self._write_state(repair=report)

        try:
            self._ensure_runtime_paths()

            with self._child_output_lock:
                child_output = list(self._child_output)
            diagnosis = self._crash_diagnoser.diagnose(
                child_output, self.project_root
            )
            report["diagnosis"] = diagnosis

            from tasks.repair_coordinator import RepairCoordinator

            failure_code = str(diagnosis.get("failure_code") or "STARTUP_CRASH")
            signal_report = RepairCoordinator(
                self.project_root
            ).request_governed_repair(
                failure_code=failure_code,
                owner="startup-sovereign",
                decision_proof={
                    "authority": "signal-only",
                    "exit_code": exit_code,
                    "uptime_seconds": round(uptime, 3),
                    "diagnosis": diagnosis,
                },
                signal_only=True,
            )
            report["ok"] = bool(signal_report.get("ok"))
            report["signal"] = signal_report
        except Exception as error:
            report["ok"] = False
            report["error"] = f"{type(error).__name__}: {error}"
        return report

    # --------------------------------------------------------------
    # supervise loop
    # --------------------------------------------------------------

    def run(self, args: list[str]) -> int:
        self._install_signals()
        self._write_state()
        while not self._stop.is_set():
            # --- five pre-spawn dependency gates (A61/E47/P26) ---
            startup = self._run_startup_phases()
            self._write_orchestrator_report(startup)

            if not startup.get("gate_ok"):
                # Required phase failed — do NOT spawn governance system (A61 GATE).
                self._status = "startup-phase-blocked"
                self._last_exit = {
                    "startup": "required-phase-failed",
                    "startup_state": startup.get("state", "FAILED"),
                    "at": _iso_now(),
                }
                self._write_state()
                # Run auto-repair (best-effort; source repair is unlikely to
                # help dependency failures, but it records the event).
                self._signal_startup_failure(1, 0.0)
                if self._restarts >= MAX_RESTARTS:
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
                    return 0
                continue

            # --- all required pre-spawn gates verified — spawn backend ---
            try:
                self._child = self._spawn_backend(
                    args,
                    startup_state=startup.get("state", ""),
                )
            except OSError as error:
                self._status = "spawn-failed"
                self._last_exit = {"error": f"{type(error).__name__}: {error}"}
                self._write_state()
                self._signal_startup_failure(1, 0.0)
                if self._restarts >= MAX_RESTARTS:
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
                    return 0
                continue

            child_started_at = time.monotonic()
            self._backend_healthy = False
            self._status = "backend-running"
            self._write_state()
            relay = threading.Thread(
                target=self._relay, args=(self._child.stdout,), daemon=True
            )
            relay.start()
            self._health_thread = threading.Thread(
                target=self._health_loop, daemon=True
            )
            self._health_thread.start()
            self._start_connection_watchdog()
            while not self._stop.is_set():
                code = self._child.poll()
                if code is not None:
                    break
                time.sleep(0.5)
            if self._stop.is_set():
                self._terminate_child()
                self._stop_connection_watchdog()
                self._status = "stopped"
                self._write_state()
                return 0
            code = int(self._child.returncode or 0)
            self._last_exit = {"code": code, "at": _iso_now()}
            if code == 0:
                self._stop_connection_watchdog()
                self._status = "backend-stopped-clean"
                self._write_state()
                return 0
            healthy_uptime = time.monotonic() - child_started_at
            if healthy_uptime >= HEALTHY_UPTIME_RESET_SECONDS:
                self._restarts = 0
            self._restarts += 1
            if self._restarts > MAX_RESTARTS:
                self._stop_connection_watchdog()
                self._status = "restart-budget-exhausted"
                self._write_state()
                return 3
            # Diagnose and signal; startup authority never mutates source.
            repair_report = self._signal_startup_failure(code, healthy_uptime)
            delay = BACKOFF_SCHEDULE_SECONDS[
                min(self._restarts - 1, len(BACKOFF_SCHEDULE_SECONDS) - 1)
            ]
            self._status = "backend-restarting"
            self._write_state(next_retry_in_seconds=delay, repair=repair_report)
            if self._stop.wait(timeout=delay):
                self._status = "stopped"
                self._write_state()
                return 0
        self._stop_connection_watchdog()
        self._status = "stopped"
        self._write_state()
        return 0


# Wire up phase handlers (avoids forward-reference issues in class body).


def main() -> int:
    return BootCore().run([*sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
