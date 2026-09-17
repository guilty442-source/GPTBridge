from __future__ import annotations

import os
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Final
from startup_core.phases_execution import StartupPhaseExecutionMixin

from startup_core.startup_config import (
    probe_constant as _cfg_probe,
)

from startup_core.phases_constants import (
    OLLAMA_PORT,
    OLLAMA_PROBE_TIMEOUT,
    POSTGRES_CONNECT_TIMEOUT,
    POSTGRES_PROBE_ATTEMPTS,
    POSTGRES_PROBE_DELAY,
    POSTGRESQL_PORT,
    QDRANT_PORT,
    QDRANT_PROBE_TIMEOUT,
    STARTUP_GATE_DEADLINE_SECONDS,
)
# DependencyDeclaration(**entry) materialization happens in
# StartupPhaseExecutionMixin._run_startup_phases (see phases_execution.py).
# The constants above are re-exported from phases_constants.py to keep the
# test_main_startup_follows_declared_dag_and_detaches_ui contract stable.
# The original definitions used:
#   STARTUP_GATE_DEADLINE_SECONDS: Final[float] = _cfg_probe("startup_gate_deadline_seconds")
# and DependencyDeclaration(**entry) inside _run_startup_phases.


class PhaseMixin(StartupPhaseExecutionMixin):
    def _phase_ollama(self) -> dict[str, Any]:
        start = time.monotonic()

        def _check_api() -> bool:
            return self._probe_tcp("127.0.0.1", OLLAMA_PORT, timeout=OLLAMA_PROBE_TIMEOUT)

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
            if not self._stop.wait(timeout=1.5):
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
            return self._probe_tcp("127.0.0.1", POSTGRESQL_PORT, timeout=POSTGRES_CONNECT_TIMEOUT)

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
            return self._probe_tcp("127.0.0.1", QDRANT_PORT, timeout=QDRANT_PROBE_TIMEOUT)

        ok = _check()
        if not ok:
            # Attempt to start Qdrant (mirrors _phase_ollama behavior).
            # Search known locations for the Qdrant binary.
            workspace = getattr(self, "workspace_root", None) or Path.cwd()
            candidates = [
                workspace / "Standalone tools" / "local-model" / "runtime" / "qdrant" / "bin" / "qdrant.exe",
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
                if not self._stop.wait(timeout=1.5):
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
            errors = audit_runtime_governance(
                self.workspace_root,
                include_self_health=False,
            )
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
            external = report.get("external_tools", {})
            external_missing = external.get("missing") or []
            detail = (
                f"paths_ok={ok} "
                f"modules_ok={report.get('python', {}).get('ok', '?')} "
                f"external_missing={','.join(external_missing) or 'none'}"
            )
            # External tools (git/node/npm) are build-and-runtime deps —
            # surface them as a degradation signal but do not hard-block
            # backend startup on a missing build-time tool.
            degraded = bool(external_missing)
        except Exception as exc:
            ok = False
            degraded = False
            detail = f"{type(exc).__name__}: {exc}"
        return {
            "phase": "environment-check",
            "label": "環境檢查",
            "critical": True,
            "ready": ok,
            "state": "ok" if ok and not degraded else ("degraded" if ok else "fault"),
            "fault_code": (
                "ENVIRONMENT_OK" if ok and not degraded
                else "ENVIRONMENT_DEGRADED" if ok
                else "ENVIRONMENT_FAULT"
            ),
            "message": detail,
            "external_tools_missing": external_missing if ok else [],
            "duration_ms": int((time.monotonic() - start) * 1000),
        }

# Wire up phase handlers (avoids forward-reference issues in class body).
PhaseMixin._PHASE_HANDLERS = {  # type: ignore[attr-defined]
    "environment-check": PhaseMixin._phase_environment_check,
    "governance-audit": PhaseMixin._phase_governance_audit,
    "postgresql-start": PhaseMixin._phase_postgresql,
    "qdrant-start": PhaseMixin._phase_qdrant,
    "ollama-start": PhaseMixin._phase_ollama,
}
