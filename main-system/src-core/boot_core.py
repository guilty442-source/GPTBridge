"""boot_core — independent startup core (啟動核心) and sole startup orchestrator (A61/E47/P26).

Entry responsibility boundary (per architecture decision):

  * 啟動入口 (Electron main) — only wakes the screen; it spawns this
    startup core and does not manage the backend directly.
  * 啟動核心 (this process) — **sole startup orchestrator** (A61):
    executes the six-phase startup sequence in order:

        environment-check  →  governance-audit  →  postgresql-start
        →  qdrant-start  →  ollama-start  →  governance-system-start

    Each required phase must be verified before the next advances (GATE).
    Then spawns the main backend (``main.py --serve``), which awakens the
    sovereigns, and supervises the backend for its whole lifetime.
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

The startup core:
  1. Runs the six-phase startup gate sequence.
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
from typing import Any, Final

MAX_RESTARTS = 10
BACKOFF_SCHEDULE_SECONDS = (2, 5, 10, 20, 30, 45, 60)
HEALTHY_UPTIME_RESET_SECONDS = 60
HEALTH_PROBE_PORT = 8765
HEALTH_PROBE_TIMEOUT = 2.0
HEALTH_PROBE_INTERVAL = 5.0
STATE_RELATIVE = ("main-system", "runtime", "state", "boot-core.json")

# Crash exit codes that trigger automatic source repair before restart.
CRASH_REPAIR_UPTIME_THRESHOLD = 30.0

# Six-phase startup sequence (A61/E47/P26).
BOOT_PHASES: Final[tuple[str, ...]] = (
    "environment-check",
    "governance-audit",
    "postgresql-start",
    "qdrant-start",
    "ollama-start",
)
# Required phases — failure gates the entire sequence (A61 GATE).
REQUIRED_BOOT_PHASES: Final[tuple[str, ...]] = (
    "environment-check",
    "governance-audit",
    "postgresql-start",
)
POSTGRES_PROBE_ATTEMPTS: Final[int] = 6
POSTGRES_PROBE_DELAY: Final[float] = 5.0
POSTGRES_CONNECT_TIMEOUT: Final[float] = 3.0
QDRANT_PROBE_TIMEOUT: Final[float] = 0.75
OLLAMA_PROBE_TIMEOUT: Final[float] = 0.75


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

    def _generate_governance_bootstrap(self) -> str:
        """Generate a fresh governance bootstrap token for main.py.

        Called before each spawn (and re-spawn) so the 30-second expiry
        window in the identity attestation is always fresh.
        """
        self._ensure_runtime_paths()

        from dataclasses import asdict
        from governance_rule.execution.authentication import (
            sign_launcher_attestation,
        )
        from governance_rule.execution.integrity import build_integrity_manifest

        workspace = self.workspace_root
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
    # TCP probe helper
    # --------------------------------------------------------------

    @staticmethod
    def _probe_tcp(host: str, port: int, timeout: float = 0.75) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    # --------------------------------------------------------------
    # six-phase startup orchestrator (A61/E47/P26)
    # --------------------------------------------------------------

    def _phase_environment_check(self) -> dict[str, Any]:
        start = time.monotonic()
        try:
            self._ensure_runtime_paths()
            from core.environment_doctor import collect_environment_report
            app_root = self.workspace_root / "main-system"
            report = collect_environment_report(app_root)
            paths = report.get("paths", {})
            ok = bool(paths.get("ok"))
            detail = f"paths_ok={ok} modules_ok={report.get('python', {}).get('ok', '?')}"
        except Exception as exc:
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        return {
            "phase": "environment-check",
            "label": "環境檢查",
            "critical": True,
            "ready": ok,
            "state": "ok" if ok else "fault",
            "fault_code": "ENVIRONMENT_OK" if ok else "ENVIRONMENT_FAULT",
            "message": detail,
            "duration_ms": int((time.monotonic() - start) * 1000),
        }

    def _phase_governance_audit(self) -> dict[str, Any]:
        start = time.monotonic()
        try:
            self._ensure_runtime_paths()
            from governance_rule.execution.audit import audit_runtime_governance
            errors = audit_runtime_governance(self.workspace_root)
            ok = len(errors) == 0
            detail = "; ".join(errors[:5]) if errors else "audit-pass"
        except Exception as exc:
            ok = False
            detail = f"{type(exc).__name__}: {exc}"
        return {
            "phase": "governance-audit",
            "label": "治理審計",
            "critical": True,
            "ready": ok,
            "state": "ok" if ok else "fault",
            "fault_code": "GOVERNANCE_AUDIT_PASS" if ok else "GOVERNANCE_AUDIT_FAULT",
            "message": detail,
            "duration_ms": int((time.monotonic() - start) * 1000),
        }

    def _phase_postgresql(self) -> dict[str, Any]:
        start = time.monotonic()
        dsn = os.environ.get("GPTBRIDGE_POSTGRES_DSN", "").strip()

        def _check() -> bool:
            if dsn:
                try:
                    import psycopg  # noqa: WPS433 — conditional import
                    with psycopg.connect(dsn, connect_timeout=int(POSTGRES_CONNECT_TIMEOUT)) as conn:
                        conn.execute("SELECT 1").fetchone()
                    return True
                except Exception:
                    pass
            return self._probe_tcp("127.0.0.1", 5432, timeout=POSTGRES_CONNECT_TIMEOUT)

        for attempt in range(POSTGRES_PROBE_ATTEMPTS):
            if self._stop.is_set():
                break
            if _check():
                return {
                    "phase": "postgresql-start",
                    "label": "啟動 PostgreSQL",
                    "critical": True,
                    "ready": True,
                    "state": "ok",
                    "fault_code": "POSTGRESQL_READY",
                    "message": "ready",
                    "duration_ms": int((time.monotonic() - start) * 1000),
                }
            if attempt < POSTGRES_PROBE_ATTEMPTS - 1:
                if self._stop.wait(timeout=POSTGRES_PROBE_DELAY):
                    break
        return {
            "phase": "postgresql-start",
            "label": "啟動 PostgreSQL",
            "critical": True,
            "ready": False,
            "state": "fault",
            "fault_code": "POSTGRESQL_UNREACHABLE",
            "message": f"not reachable after {POSTGRES_PROBE_ATTEMPTS} attempts",
            "duration_ms": int((time.monotonic() - start) * 1000),
        }

    def _phase_qdrant(self) -> dict[str, Any]:
        start = time.monotonic()
        ok = self._probe_tcp("127.0.0.1", 6333, timeout=QDRANT_PROBE_TIMEOUT)
        return {
            "phase": "qdrant-start",
            "label": "啟動 Qdrant",
            "critical": False,
            "ready": ok,
            "state": "ok" if ok else "degraded",
            "fault_code": "QDRANT_READY" if ok else "QDRANT_UNREACHABLE",
            "message": "ready" if ok else "not reachable (degradable)",
            "duration_ms": int((time.monotonic() - start) * 1000),
        }

    def _phase_ollama(self) -> dict[str, Any]:
        start = time.monotonic()

        def _check_api() -> bool:
            return self._probe_tcp("127.0.0.1", 11434, timeout=OLLAMA_PROBE_TIMEOUT)

        ok = _check_api()
        if not ok:
            # Attempt to start Ollama (mirrors startup_orchestrator behavior).
            appdata = os.environ.get("LOCALAPPDATA", "")
            ollama_root = Path(appdata) / "Programs" / "Ollama"
            app = ollama_root / "ollama app.exe"
            server = ollama_root / "ollama.exe"
            creationflags = (
                int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
                | int(getattr(subprocess, "DETACHED_PROCESS", 0) or 0)
            )
            try:
                cmd = (
                    [str(app)] if app.is_file()
                    else [str(server), "serve"] if server.is_file()
                    else None
                )
                if cmd:
                    subprocess.Popen(  # noqa: S603 — governed local tool spawn
                        cmd,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=creationflags,
                    )
            except Exception:
                pass
            if not self._stop.wait(timeout=3.0):
                ok = _check_api()
        return {
            "phase": "ollama-start",
            "label": "啟動 Ollama",
            "critical": False,
            "ready": ok,
            "state": "ok" if ok else "degraded",
            "fault_code": "OLLAMA_READY" if ok else "OLLAMA_UNREACHABLE",
            "message": "ready" if ok else "not reachable (degradable)",
            "duration_ms": int((time.monotonic() - start) * 1000),
        }

    _PHASE_HANDLERS: Final[dict[str, Any]] = {}  # populated after class body below

    def _run_startup_phases(self) -> dict[str, Any]:
        """Execute the six-phase startup sequence per A61/E47/P26.

        Returns a report dict containing phase results, startup state,
        and gate_ok (True = safe to spawn governance system).
        """
        total_start = time.monotonic()
        results: list[dict[str, Any]] = []
        gate_ok = True
        handlers = self._PHASE_HANDLERS

        for phase in BOOT_PHASES:
            if self._stop.is_set():
                gate_ok = False
                break
            result = handlers[phase](self)
            results.append(result)
            # Required phase failed → gate blocks subsequent phases (A61 GATE).
            if result.get("critical") and not result.get("ready"):
                gate_ok = False
                break

        total_ms = int((time.monotonic() - total_start) * 1000)

        postgres_ok = next((r["ready"] for r in results if r["phase"] == "postgresql-start"), False)
        qdrant_ok = next((r["ready"] for r in results if r["phase"] == "qdrant-start"), False)
        ollama_ok = next((r["ready"] for r in results if r["phase"] == "ollama-start"), False)

        if not gate_ok:
            startup_state = "FAILED"
        elif not (qdrant_ok and ollama_ok):
            startup_state = "DEGRADED"
        else:
            startup_state = "READY"

        exit_code = 0 if startup_state in ("READY", "DEGRADED") else 2

        report: dict[str, Any] = {
            "state": startup_state,
            "startup_order": list(BOOT_PHASES),
            "critical_services": ["postgresql"],
            "degradable_services": ["qdrant", "ollama"],
            "postgresql": next((r for r in results if r["phase"] == "postgresql-start"), {}),
            "qdrant": next((r for r in results if r["phase"] == "qdrant-start"), {}),
            "ollama": next((r for r in results if r["phase"] == "ollama-start"), {}),
            "environment": next((r for r in results if r["phase"] == "environment-check"), {}),
            "governance_audit": next((r for r in results if r["phase"] == "governance-audit"), {}),
            "exit_code": exit_code,
            "total_duration_ms": total_ms,
            "gate_ok": gate_ok,
            "phases": results,
        }
        return report

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
            self._ensure_runtime_paths()
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
            # --- six-phase startup gate (A61/E47/P26) ---
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

            # --- all required phases verified — spawn governance system ---
            try:
                self._child = self._spawn_backend(
                    args,
                    startup_state=startup.get("state", ""),
                )
            except OSError as error:
                self._status = "spawn-failed"
                self._last_exit = {"error": f"{type(error).__name__}: {error}"}
                self._write_state()
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


# Wire up phase handlers (avoids forward-reference issues in class body).
BootCore._PHASE_HANDLERS = {  # type: ignore[attr-defined]
    "environment-check": BootCore._phase_environment_check,
    "governance-audit": BootCore._phase_governance_audit,
    "postgresql-start": BootCore._phase_postgresql,
    "qdrant-start": BootCore._phase_qdrant,
    "ollama-start": BootCore._phase_ollama,
}


def main() -> int:
    return BootCore().run([*sys.argv[1:]])


if __name__ == "__main__":
    raise SystemExit(main())
