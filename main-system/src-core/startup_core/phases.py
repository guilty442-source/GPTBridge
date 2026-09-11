from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Final

# ------------------------------------------------------------------
# Startup phase constants
# ------------------------------------------------------------------

OLLAMA_PROBE_TIMEOUT: Final[float] = 2.0
POSTGRES_CONNECT_TIMEOUT: Final[float] = 2.0
POSTGRES_PROBE_ATTEMPTS: Final[int] = 5
POSTGRES_PROBE_DELAY: Final[float] = 1.0
QDRANT_PROBE_TIMEOUT: Final[float] = 2.0

BOOT_PHASES: Final[tuple[str, ...]] = (
    "environment-check",
    "governance-audit",
    "postgresql-start",
    "qdrant-start",
    "ollama-start",
)


class PhaseMixin:
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
    def _probe_tcp(self, host: str, port: int, timeout: float = 0.75) -> bool:
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False
    def _phase_qdrant(self) -> dict[str, Any]:
        start = time.monotonic()

        def _check() -> bool:
            return self._probe_tcp("127.0.0.1", 6333, timeout=QDRANT_PROBE_TIMEOUT)

        ok = _check()
        if not ok:
            # Attempt to start Qdrant (mirrors _phase_ollama behavior).
            # Search known locations for the Qdrant binary.
            workspace = getattr(self, "workspace_root", None) or Path.cwd()
            candidates = [
                workspace / "local-model" / "runtime" / "qdrant" / "bin" / "qdrant.exe",
                Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "qdrant" / "qdrant.exe",
            ]
            qdrant_exe = next((c for c in candidates if c.is_file()), None)
            if qdrant_exe is not None:
                qdrant_root = qdrant_exe.parents[1]  # .../qdrant/
                creationflags = (
                    int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
                    | int(getattr(subprocess, "DETACHED_PROCESS", 0) or 0)
                )
                try:
                    subprocess.Popen(  # noqa: S603 — governed local tool spawn
                        [str(qdrant_exe)],
                        cwd=str(qdrant_root),
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=creationflags,
                    )
                except Exception:
                    pass
                if not self._stop.wait(timeout=5.0):
                    ok = _check()
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

# Wire up phase handlers (avoids forward-reference issues in class body).
PhaseMixin._PHASE_HANDLERS = {  # type: ignore[attr-defined]
    "environment-check": PhaseMixin._phase_environment_check,
    "governance-audit": PhaseMixin._phase_governance_audit,
    "postgresql-start": PhaseMixin._phase_postgresql,
    "qdrant-start": PhaseMixin._phase_qdrant,
    "ollama-start": PhaseMixin._phase_ollama,
}
