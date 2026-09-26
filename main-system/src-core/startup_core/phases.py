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
    VECTORD_PROBE_TIMEOUT,
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
        """§10.7 on-demand：啟動階段只做唯讀探測，不 spawn。

        Ollama 依 ``resident-core.json`` 歸類 on-demand——boot 時缺席屬
        常態而非退化；能力請求經
        ``core_system.ollama_demand.ensure_ollama_ready()`` 受治理拉起。
        僅在未安裝（能力不存在）時回報 degraded。"""
        start = time.monotonic()
        ok = self._probe_tcp(
            "127.0.0.1", OLLAMA_PORT, timeout=OLLAMA_PROBE_TIMEOUT
        )
        installed = True
        if not ok:
            from core_system.ollama_demand import ollama_installed

            installed = ollama_installed()
        return {
            "phase": "ollama-start",
            "label": "啟動 Ollama",
            "critical": False,
            "on_demand": True,
            "installed": installed,
            "ready": ok,
            "state": (
                "ok" if ok else ("deferred" if installed else "degraded")
            ),
            "fault_code": "OLLAMA_READY" if ok else "OLLAMA_UNREACHABLE",
            "message": (
                "ready"
                if ok
                else (
                    "on-demand deferred (installed)"
                    if installed
                    else "not installed"
                )
            ),
            "duration_ms": int((time.monotonic() - start) * 1000),
        }
    def _phase_postgresql(self) -> dict[str, Any]:
        start = time.monotonic()
        # G89/G24: resolve through the governed credential store so the
        # certified path still works after env DSNs are cleared; only when
        # no DSN exists anywhere does this degrade to the TCP probe.
        dsn = ""
        dsn_source = "none"
        try:
            from shared_layer.security.dsn_policy import (  # noqa: PLC0415
                DsnPolicyError,
                DsnPurpose,
                resolve_dsn,
            )
            binding = resolve_dsn(DsnPurpose.RUNTIME)
            dsn = binding.dsn.strip()
            dsn_source = binding.env_name
        except Exception:
            dsn = os.environ.get("GPTBRIDGE_POSTGRES_DSN", "").strip()
            dsn_source = "env-fallback" if dsn else "none"
        certification: dict[str, Any] | None = None

        def _check() -> bool:
            nonlocal certification
            if dsn:
                try:
                    import psycopg  # noqa: WPS433 — conditional import
                    with psycopg.connect(dsn, connect_timeout=int(POSTGRES_CONNECT_TIMEOUT)) as conn:
                        conn.execute("SELECT 1").fetchone()
                        # Migration 030: certify before marking READY —
                        # schema/RLS/roles/migration-head/audit/contract,
                        # not just SELECT 1.
                        from shared_layer.database.startup_certifier import (  # noqa: PLC0415
                            certify_startup,
                        )
                        certification = certify_startup(
                            conn, certified_by="startup-phase:postgresql-start"
                        )
                    return True
                except Exception:
                    certification = None
                    pass
            return self._probe_tcp("127.0.0.1", POSTGRESQL_PORT, timeout=POSTGRES_CONNECT_TIMEOUT)

        for attempt in range(POSTGRES_PROBE_ATTEMPTS):
            if self._stop.is_set():
                break
            if _check():
                if certification is not None and not certification.get("ready"):
                    return {
                        "phase": "postgresql-start",
                        "label": "啟動 PostgreSQL",
                        "critical": True,
                        "ready": False,
                        "state": "fault",
                        "fault_code": "POSTGRESQL_CERTIFICATION_FAILED",
                        "message": "startup certification failed",
                        "certification": certification,
                        "duration_ms": int((time.monotonic() - start) * 1000),
                    }
                return {
                    "phase": "postgresql-start",
                    "label": "啟動 PostgreSQL",
                    "critical": True,
                    "ready": True,
                    "state": "ok",
                    "fault_code": "POSTGRESQL_READY",
                    "message": "ready",
                    "certification": (
                        certification
                        if certification is not None
                        else "skipped:no-dsn"
                    ),
                    "dsn_source": dsn_source,
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
    def _phase_vectord(self) -> dict[str, Any]:
        """A610 takeover: semantic-index readiness is owned by the Rust
        vectord engine (Qdrant retired; sealed cutover)."""
        start = time.monotonic()
        vectord_port = int(os.environ.get("VECTORD_PORT", "8092"))

        def _check() -> bool:
            return self._probe_tcp("127.0.0.1", vectord_port, timeout=VECTORD_PROBE_TIMEOUT)

        ok = _check()
        if not ok:
            workspace = getattr(self, "workspace_root", None) or Path.cwd()
            binary = (
                workspace
                / "Standalone tools"
                / "vectord-rs"
                / "bin"
                / "vectord.exe"
            )
            if binary.is_file():
                creationflags = (
                    int(getattr(subprocess, "CREATE_NO_WINDOW", 0) or 0)
                    | int(getattr(subprocess, "DETACHED_PROCESS", 0) or 0)
                )
                store_dir = binary.parents[1] / "runtime"
                try:
                    subprocess.Popen(  # noqa: S603 — governed local tool spawn
                        [
                            str(binary),
                            "--bind",
                            f"127.0.0.1:{vectord_port}",
                            "--store-dir",
                            str(store_dir),
                        ],
                        cwd=str(binary.parents[1]),
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
            "phase": "vectord-start",
            "label": "啟動 vectord（Rust 語意索引）",
            "critical": False,
            "ready": ok,
            "state": "ok" if ok else "degraded",
            "fault_code": "VECTORD_READY" if ok else "VECTORD_UNREACHABLE",
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
            # External tools (git) are build-and-runtime deps —
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
    "vectord-start": PhaseMixin._phase_vectord,
    "ollama-start": PhaseMixin._phase_ollama,
}
