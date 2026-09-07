"""boot_core — independent startup core (啟動核心).

Entry responsibility boundary (per architecture decision):

  * 啟動入口 (Electron main) — only wakes the screen; it spawns this
    startup core and does not manage the backend directly.
  * 啟動核心 (this process) — awakens the system core: it generates the
    governance bootstrap token, spawns the main backend (``main.py --serve``),
    which in turn awakens the sovereigns, and supervises the backend for its
    whole lifetime.
  * Crash recovery — when the backend crashes with a non-zero exit code
    during startup (uptime < 30s), the startup core runs automatic source
    self-repair (``CentralRepairService.self_repair_main_system_sources``)
    before restarting.  This fixes corrupted source files (e.g. SyntaxError,
    IndentationError) that would otherwise cause repeated crash loops.
  * Independent tool isolation — independent tools (非常駐服務) are spawned
    in their own process group (CREATE_NEW_PROCESS_GROUP) so they survive a
    main-system crash or restart.  The startup core only terminates the
    main backend process itself, never the independent tool processes.
  * Single-fault isolation — a backend crash is restarted here with bounded
    backoff; if THIS process dies, the launcher's own recovery respawns it
    while the backend (if still alive) keeps serving.

The startup core generates the governance bootstrap token in-process (so the
entry does not need governance knowledge), forwards the environment to the
backend, relays the child's stdout/stderr so the launcher can observe
readiness lines, and persists a small status file for maintenance oversight.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

MAX_RESTARTS = 10
BACKOFF_SCHEDULE_SECONDS = (2, 5, 10, 20, 30, 45, 60)
HEALTHY_UPTIME_RESET_SECONDS = 60
HEALTH_PROBE_PORT = 8765
HEALTH_PROBE_TIMEOUT = 2.0
HEALTH_PROBE_INTERVAL = 5.0
STATE_RELATIVE = ("main-system", "runtime", "state", "boot-core.json")

# Crash exit codes that trigger automatic source repair before restart.
# A non-zero exit with a short uptime suggests a startup-time failure that
# may be caused by corrupted source (e.g. SyntaxError, IndentationError).
CRASH_REPAIR_UPTIME_THRESHOLD = 30.0


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BootCore:
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

    # --------------------------------------------------------------
    # governance bootstrap
    # --------------------------------------------------------------

    def _generate_governance_bootstrap(self) -> str:
        """Generate a fresh governance bootstrap token for main.py.

        Called before each spawn (and re-spawn) so the 30-second expiry
        window in the identity attestation is always fresh.
        """
        workspace = self.workspace_root
        sys.path.insert(0, str(workspace))
        sys.path.insert(0, str(workspace / "main-system" / "src-core"))
        sys.path.insert(0, str(workspace / "shared-layer" / "src"))

        from dataclasses import asdict
        from governance_rule.execution.authentication import (
            sign_launcher_attestation,
        )
        from governance_rule.execution.integrity import build_integrity_manifest

        launcher_key = secrets.token_bytes(32)
        issued_at = int(time.time())
        key_id = secrets.token_hex(16)
        integrity = build_integrity_manifest(
            workspace, launcher_key, issued_at=issued_at, key_id=key_id
        )
        attestation = sign_launcher_attestation(
            launcher_key,
            actor="governance/main-system",
            bound_tool_id="main-system",
            caller_path="main-system/src-core/main.py",
            process_id=os.getpid(),
            issued_at=issued_at,
            key_id=key_id,
        )
        payload = {
            "format_version": 1,
            "launcher_key": base64.b64encode(launcher_key).decode("ascii"),
            "integrity_manifest": asdict(integrity),
            "identity_attestation": asdict(attestation),
        }
        return base64.b64encode(
            json.dumps(payload, separators=(",", ":")).encode("utf-8")
        ).decode("ascii")

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
    # child lifecycle
    # --------------------------------------------------------------

    def _spawn_backend(self, args: list[str]) -> subprocess.Popen[bytes]:
        command = [
            os.fspath(Path(sys.executable).resolve()),
            "-u",
            "-B",
            os.fspath(self.backend_entry),
            *args,
        ]
        creationflags = int(
            getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0
        )
        env = dict(os.environ)
        # Generate a fresh governance bootstrap token for each spawn so the
        # 30-second identity attestation expiry is always within window.
        if not env.get("GPTBRIDGE_GOVERNANCE_BOOTSTRAP"):
            try:
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
        return subprocess.Popen(  # noqa: S603 - governed local spawn
            command,
            cwd=os.fspath(self.project_root),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )

    def _probe_health(self) -> bool:
        """Probe the backend HTTP /health endpoint."""
        try:
            request = urllib.request.Request(
                f"http://127.0.0.1:{HEALTH_PROBE_PORT}/health",
                headers={"Connection": "close"},
            )
            with urllib.request.urlopen(
                request, timeout=HEALTH_PROBE_TIMEOUT
            ) as response:
                return 200 <= response.status < 300
        except (OSError, urllib.error.URLError):
            return False

    def _health_loop(self) -> None:
        """Background thread: periodically probe backend health for state file."""
        while not self._stop.is_set():
            if self._child is None or self._child.poll() is not None:
                break
            healthy = self._probe_health()
            if healthy != self._backend_healthy:
                self._backend_healthy = healthy
                self._write_state(backend_healthy=healthy)
            if self._stop.wait(timeout=HEALTH_PROBE_INTERVAL):
                break

    def _start_connection_watchdog(self) -> None:
        """Start the connection watchdog thread."""
        try:
            sys.path.insert(0, str(self.workspace_root))
            sys.path.insert(0, str(self.project_root / "main-system" / "src-core"))
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

        This triggers a CentralRepairService repair for FRONTEND_BACKEND_DISCONNECTED,
        which records the event in the learning store and may trigger source repair.
        """
        try:
            sys.path.insert(0, str(self.workspace_root))
            sys.path.insert(0, str(self.project_root / "main-system" / "src-core"))
            from tasks.central_repair import CentralRepairService

            repair_root = self.project_root / "main-system" / "data" / "automatic-repair"
            repair_root.mkdir(parents=True, exist_ok=True)
            service = CentralRepairService(self.project_root, repair_root)
            # Record the connection failure for learning.
            service._learn_from_tool_repair(
                "main-system", failure_code,
                {
                    "run_id": f"watchdog-{int(time.time())}",
                    "ok": False,
                    "executed_actions": ["connection-watchdog"],
                    "failure_code": failure_code,
                },
            )
        except Exception:
            pass  # Learning is best-effort.

    def _relay(self, stream: object) -> None:
        """Forward child output so the launcher sees readiness lines."""

        try:
            for raw in iter(stream.readline, b""):
                try:
                    sys.stdout.buffer.write(raw)
                    sys.stdout.buffer.flush()
                except (BrokenPipeError, OSError):
                    return
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

    def _run_auto_repair(self, exit_code: int, uptime: float) -> dict[str, object]:
        """Run source self-repair when the backend crashes during startup.

        Returns a repair report dict.  The repair is best-effort: if it fails,
        the restart still proceeds (the backend may crash again, but the
        restart budget will eventually exhaust).

        Learning integration: after each repair attempt, the outcome is
        recorded in the learning store.  Recurring error→remedy patterns
        are auto-promoted to learned recipes for future dispatch.
        """

        report: dict[str, object] = {
            "triggered": False,
            "reason": "",
            "ok": False,
        }
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
            sys.path.insert(0, str(self.workspace_root))
            sys.path.insert(0, str(self.project_root / "main-system" / "src-core"))
            sys.path.insert(0, str(self.workspace_root / "shared-layer" / "src"))
            from tasks.central_repair import CentralRepairService

            repair_root = self.project_root / "main-system" / "data" / "automatic-repair"
            repair_root.mkdir(parents=True, exist_ok=True)
            service = CentralRepairService(self.project_root, repair_root)
            # Consult learned recipes before attempting repair.
            try:
                suggestion = service.suggest_remedy_for_error(
                    "BackendCrash",
                    f"exit_code={exit_code} uptime={uptime:.1f}s",
                    file_path="main-system/src-core/main.py",
                )
                if suggestion.get("suggested"):
                    report["learned_suggestion"] = suggestion
            except Exception:
                pass
            result = service.self_repair_main_system_sources()
            report["ok"] = bool(result.get("ok"))
            report["result"] = result
            # Learning is integrated inside self_repair_main_system_sources;
            # the learner records errors and outcomes automatically.
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
            try:
                self._child = self._spawn_backend(args)
            except OSError as error:
                self._status = "spawn-failed"
                self._last_exit = {"error": f"{type(error).__name__}: {error}"}
                self._write_state()
                # Attempt auto-repair on spawn failure (e.g. corrupted
                # governance bootstrap source) before giving up.
                self._run_auto_repair(1, 0.0)
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
            # Run automatic source repair on early crashes before restarting.
            repair_report = self._run_auto_repair(code, healthy_uptime)
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


def main() -> int:
    return BootCore().run([*sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
