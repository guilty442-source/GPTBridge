from __future__ import annotations

import os
import socket
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Final

from startup_core.startup_config import (
    bootstrap_phases as _cfg_bootstrap_phases,
    dependency_manifest as _cfg_dependency_manifest,
    port as _cfg_port,
    probe_constant as _cfg_probe,
)

# ------------------------------------------------------------------
# Startup phase constants — loaded from config/startup_manifest.json
# (A191/A192: no longer hardcoded; editable without source changes)
# ------------------------------------------------------------------

OLLAMA_PROBE_TIMEOUT: Final[float] = _cfg_probe("ollama_probe_timeout")
POSTGRES_CONNECT_TIMEOUT: Final[float] = _cfg_probe("postgres_connect_timeout")
POSTGRES_PROBE_ATTEMPTS: Final[int] = _cfg_probe("postgres_probe_attempts")
POSTGRES_PROBE_DELAY: Final[float] = _cfg_probe("postgres_probe_delay")
QDRANT_PROBE_TIMEOUT: Final[float] = _cfg_probe("qdrant_probe_timeout")
STARTUP_GATE_DEADLINE_SECONDS: Final[float] = _cfg_probe("startup_gate_deadline_seconds")

BOOTSTRAP_PHASES: Final[tuple[str, ...]] = _cfg_bootstrap_phases()

# Current certified dependency contracts. Criticality is declared by the
# consuming contract, never inferred from a service name.
#
# Loaded from ``config/startup_manifest.json`` so new dependencies can be
# added without source-code changes.  The declarations are materialized
# into ``DependencyDeclaration`` objects at runtime inside
# ``_run_startup_phases``.
DEPENDENCY_MANIFEST: Final[tuple[dict[str, Any], ...]] = _cfg_dependency_manifest()

# Port constants — loaded from config (A191/A192)
OLLAMA_PORT: Final[int] = _cfg_port("ollama")
POSTGRESQL_PORT: Final[int] = _cfg_port("postgresql")
QDRANT_PORT: Final[int] = _cfg_port("qdrant")


class PhaseMixin:
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
        """Execute bootstrap gates and the certified dependency DAG.

        Returns a report dict containing phase results, startup state,
        and gate_ok (True = safe to spawn governance system).
        """
        total_start = time.monotonic()
        results: list[dict[str, Any]] = []
        gate_ok = True
        handlers = self._PHASE_HANDLERS

        for phase in BOOTSTRAP_PHASES:
            if self._stop.is_set():
                gate_ok = False
                break
            result = handlers[phase](self)
            results.append(result)
            # Required phase failed → gate blocks subsequent phases (A61 GATE).
            if result.get("critical") and not result.get("ready"):
                gate_ok = False
                break

        # The governed-startup contracts live in core_system, which is only
        # importable after the bootstrap phases above install the runtime
        # paths.  Materialize the certified manifest lazily here.
        self._ensure_runtime_paths()
        declarations: tuple[Any, ...] = ()
        dag: Any = None
        classification: dict[str, Any] = {"ok": False, "violations": ["import-unavailable"]}
        try:
            from core_system.governed_startup import (  # noqa: PLC0415
                DependencyDAG,
                DependencyDeclaration,
                verify_dependency_classification,
            )
            declarations = tuple(
                DependencyDeclaration(**entry) for entry in DEPENDENCY_MANIFEST
            )
            dag = DependencyDAG(declarations)
            classification = verify_dependency_classification(dag)
        except Exception as error:
            classification = {
                "ok": False,
                "basis": "A191/E166",
                "violations": [f"{type(error).__name__}: {error}"],
            }
        if gate_ok and (
            dag is None or not dag.is_acyclic or not classification["ok"]
        ):
            gate_ok = False

        if gate_ok:
            phase_by_identity = {
                "postgresql": "postgresql-start",
                "qdrant": "qdrant-start",
                "ollama": "ollama-start",
            }
            with ThreadPoolExecutor(
                max_workers=len(declarations),
                thread_name_prefix="startup-dag",
            ) as executor:
                futures = {
                    executor.submit(handlers[phase_by_identity[dep.identity]], self): dep
                    for dep in declarations
                }
                for future in as_completed(futures):
                    dep = futures[future]
                    try:
                        result = future.result()
                    except Exception as error:
                        result = {
                            "phase": phase_by_identity[dep.identity],
                            "ready": False,
                            "state": "fault",
                            "fault_code": "STARTUP_DEPENDENCY_EXCEPTION",
                            "message": f"{type(error).__name__}: {error}",
                            "duration_ms": 0,
                        }
                    result["criticality"] = dep.criticality
                    result["required_by"] = dep.required_by
                    results.append(result)
                    if dep.is_core_critical and not result.get("ready"):
                        gate_ok = False

        total_ms = int((time.monotonic() - total_start) * 1000)
        deadline_exceeded = total_ms > int(STARTUP_GATE_DEADLINE_SECONDS * 1000)
        if deadline_exceeded:
            gate_ok = False

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
            "startup_order": list(BOOTSTRAP_PHASES) + [
                "certified-dependency-dag"
            ],
            "dependency_dag": dag.as_dict() if dag is not None else {},
            "dependency_classification": classification,
            "critical_services": [
                dep.identity for dep in declarations if dep.is_core_critical
            ],
            "degradable_services": [
                dep.identity for dep in declarations if not dep.is_core_critical
            ],
            "postgresql": next((r for r in results if r["phase"] == "postgresql-start"), {}),
            "qdrant": next((r for r in results if r["phase"] == "qdrant-start"), {}),
            "ollama": next((r for r in results if r["phase"] == "ollama-start"), {}),
            "environment": next((r for r in results if r["phase"] == "environment-check"), {}),
            "governance_audit": next((r for r in results if r["phase"] == "governance-audit"), {}),
            "exit_code": exit_code,
            "total_duration_ms": total_ms,
            "deadline_ms": int(STARTUP_GATE_DEADLINE_SECONDS * 1000),
            "deadline_exceeded": deadline_exceeded,
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
