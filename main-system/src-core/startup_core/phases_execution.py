"""Startup phase execution mixin (A185 split).

Contains the _run_startup_phases method extracted from PhaseMixin.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from startup_core.phases_constants import (
    BOOTSTRAP_PHASES,
    DEPENDENCY_MANIFEST,
    STARTUP_GATE_DEADLINE_SECONDS,
)


class StartupPhaseExecutionMixin:
    """Startup phase execution and DAG verification."""

    _stop: Any
    _PHASE_HANDLERS: dict[str, Any]

    def _ensure_runtime_paths(self) -> None:
        raise NotImplementedError

    def _run_startup_phases(self) -> dict[str, Any]:
        """Execute bootstrap gates and the certified dependency DAG."""
        total_start = time.monotonic()
        results: list[dict[str, Any]] = []
        handlers = self._PHASE_HANDLERS

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

        phase_by_identity = {
            "postgresql": "postgresql-start",
            "qdrant": "qdrant-start",
            "ollama": "ollama-start",
        }
        bootstrap_results: dict[str, dict[str, Any]] = {}
        dependency_results: dict[str, dict[str, Any]] = {}
        if not self._stop.is_set():
            workers = max(1, len(BOOTSTRAP_PHASES) + len(declarations))
            with ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="startup-dag",
            ) as executor:
                futures: dict[Any, tuple[str, Any]] = {}
                for phase in BOOTSTRAP_PHASES:
                    futures[executor.submit(handlers[phase], self)] = (
                        "bootstrap",
                        phase,
                    )
                for dep in declarations:
                    futures[
                        executor.submit(
                            handlers[phase_by_identity[dep.identity]], self
                        )
                    ] = ("dependency", dep)
                for future in as_completed(futures):
                    kind, tag = futures[future]
                    try:
                        result = future.result()
                    except Exception as error:
                        result = {
                            "phase": (
                                tag
                                if kind == "bootstrap"
                                else phase_by_identity[tag.identity]
                            ),
                            "ready": False,
                            "state": "fault",
                            "fault_code": "STARTUP_DEPENDENCY_EXCEPTION",
                            "message": f"{type(error).__name__}: {error}",
                            "duration_ms": 0,
                        }
                    if kind == "bootstrap":
                        bootstrap_results[tag] = result
                    else:
                        result["criticality"] = tag.criticality
                        result["required_by"] = tag.required_by
                        dependency_results[tag.identity] = result

        # Deterministic report order: manifest order, not completion order.
        results = [
            *(bootstrap_results[phase] for phase in BOOTSTRAP_PHASES
              if phase in bootstrap_results),
            *(dependency_results[dep.identity] for dep in declarations
              if dep.identity in dependency_results),
        ]

        gate_ok = not self._stop.is_set()
        for phase in BOOTSTRAP_PHASES:
            result = bootstrap_results.get(phase)
            if result is None:
                gate_ok = False
            elif result.get("critical") and not result.get("ready"):
                gate_ok = False
        if dag is None or not dag.is_acyclic or not classification["ok"]:
            gate_ok = False
        for dep in declarations:
            result = dependency_results.get(dep.identity)
            if dep.is_core_critical and not (result and result.get("ready")):
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


__all__ = ["StartupPhaseExecutionMixin"]
