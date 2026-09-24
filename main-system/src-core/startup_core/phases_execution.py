"""Startup phase execution mixin (A185 split).

Contains the _run_startup_phases method extracted from PhaseMixin.
"""
from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from startup_core.phases_constants import (
    BOOTSTRAP_PHASES,
    DEPENDENCY_MANIFEST,
    STARTUP_GATE_DEADLINE_SECONDS,
)


# AA2: bound the startup worker pool regardless of manifest size.
STARTUP_PHASE_MAX_WORKERS = 8

# Module-private SQLite stores that phase-4 (PRIVATE_STATE_READY) must
# verify: transport outbox, update/repair state, governance nonces and
# the toolbox registry.  Each must open read-only and pass a quick
# integrity check — a corrupt private store must stop the ladder, not
# be silently skipped.
_PRIVATE_STATE_STORES: Final[tuple[str, ...]] = (
    "state-outbox",
    "updates",
    "governance_authentication",
    "gptbridge",
)


def _probe_private_state(state_root: Any) -> dict[str, Any]:
    """PRIVATE_STATE_READY evidence: per-store existence + integrity."""
    import sqlite3  # noqa: PLC0415

    detail: dict[str, Any] = {"stores": {}, "probed": 0}
    ok = True
    for name in _PRIVATE_STATE_STORES:
        path = state_root / f"{name}.sqlite3"
        if not path.is_file():
            detail["stores"][name] = "absent"
            continue
        detail["probed"] += 1
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                verdict = conn.execute("PRAGMA quick_check(1)").fetchone()
            finally:
                conn.close()
            healthy = bool(verdict and verdict[0] == "ok")
        except Exception as error:  # noqa: BLE001 — probe must not raise
            healthy = False
            detail["stores"][name] = f"fault:{type(error).__name__}: {error}"
            ok = False
            continue
        detail["stores"][name] = "ok" if healthy else f"corrupt:{verdict[0]}"
        ok = ok and healthy
    detail["ready"] = ok and detail["probed"] > 0
    return detail


def _probe_recovery(state_root: Any) -> dict[str, Any]:
    """RECOVERY_READY evidence: transport outbox backlog is inspectable."""
    import sqlite3  # noqa: PLC0415

    outbox = state_root / "state-outbox.sqlite3"
    if not outbox.is_file():
        return {"ready": False, "reason": "outbox-absent"}
    try:
        conn = sqlite3.connect(f"file:{outbox}?mode=ro", uri=True)
        try:
            pending = conn.execute(
                "SELECT COUNT(*) FROM outbox_events WHERE committed_at IS NULL"
            ).fetchone()[0]
            total = conn.execute("SELECT COUNT(*) FROM outbox_events").fetchone()[0]
        finally:
            conn.close()
    except Exception as error:  # noqa: BLE001 — probe must not raise
        return {"ready": False, "reason": f"{type(error).__name__}: {error}"}
    return {"ready": True, "pending_events": pending, "total_events": total}


def _dependency_start_order(declarations: tuple[Any, ...]) -> list[Any]:
    """Order declarations so prerequisites are submitted first.

    ``required_by`` names the consuming identity.  When that consumer is
    itself a declared dependency, the required service must start earlier —
    with a bounded pool, submission order decides which tasks run first.
    Depth = length of the consumer chain above the declaration (deepest
    chain first).  Cycles degrade gracefully to depth 0 (the DAG's own
    ``is_acyclic`` check still reports them).
    """
    by_identity = {d.identity: d for d in declarations}
    depth: dict[str, int] = {}

    def _depth(identity: str, seen: frozenset[str]) -> int:
        if identity in depth:
            return depth[identity]
        dep = by_identity.get(identity)
        if dep is None or dep.required_by not in by_identity or identity in seen:
            depth[identity] = 0
        else:
            depth[identity] = 1 + _depth(dep.required_by, seen | {identity})
        return depth[identity]

    return sorted(declarations, key=lambda d: -_depth(d.identity, frozenset()))


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
            # AA2: bounded worker pool (never one worker per declaration)
            # plus dependency-aware submission order — a declaration that
            # another declared service requires starts earliest.
            from shared_layer.performance.thread_budget import (
                bounded_workers,
            )

            workers = bounded_workers(
                min(
                    STARTUP_PHASE_MAX_WORKERS,
                    len(BOOTSTRAP_PHASES) + len(declarations),
                )
            )
            ordered_deps = _dependency_start_order(declarations)
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
                for dep in ordered_deps:
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
        # §10.7 on-demand：Ollama 缺席僅在未安裝（能力不存在）時降級；
        # 已安裝但未運行屬 deferred 常態，等待明確需求拉起。
        ollama_installed = next(
            (
                bool(r.get("installed", True))
                for r in results
                if r["phase"] == "ollama-start"
            ),
            True,
        )
        governance_ok = next((r["ready"] for r in results if r["phase"] == "governance-audit"), False)
        environment_ok = next((r["ready"] for r in results if r["phase"] == "environment-check"), False)
        postgres_cert = next(
            (r.get("certification") for r in results if r["phase"] == "postgresql-start"),
            None,
        )
        central_authority_ok = bool(
            postgres_ok
            and isinstance(postgres_cert, dict)
            and postgres_cert.get("ready")
        )

        # Migration-030 ladder: advance the ten-rung StartupGate with the
        # evidence each runtime phase actually produced.  The ladder is
        # strictly ordered — rungs without a real evidence source leave it
        # stopped exactly where the evidence ends (never fabricated).
        ladder_reached: list[str] = []
        ladder_fault = ""
        ladder_evidence: dict[str, Any] = {}
        try:
            from shared_layer.startup_gate import (  # noqa: PLC0415
                StartupGate,
                StartupGateError,
                StartupPhase,
            )

            state_root = (
                self.workspace_root / "main-system" / "runtime" / "state"
            )
            private_state = _probe_private_state(state_root)
            recovery = _probe_recovery(state_root)
            ladder_evidence["private_state"] = private_state
            ladder_evidence["recovery"] = recovery

            readiness_path = state_root / "runtime-readiness.json"
            read_model_ok = False
            try:
                snapshot = json.loads(readiness_path.read_text(encoding="utf-8"))
                read_model_ok = isinstance(snapshot.get("snapshot"), dict)
            except (OSError, ValueError):
                pass
            ladder_evidence["read_model"] = {
                "ready": read_model_ok,
                "path": str(readiness_path),
            }

            ladder = StartupGate()
            rung_evidence = [
                (StartupPhase.BOOTSTRAP, True),
                (StartupPhase.GOVERNANCE_VALIDATED, governance_ok),
                (StartupPhase.SECURITY_VALIDATED, environment_ok),
                (StartupPhase.DATABASE_FOUNDATION_READY, postgres_ok),
                (StartupPhase.CENTRAL_AUTHORITY_READY, central_authority_ok),
                (StartupPhase.MODULE_PRIVATE_READY, private_state["ready"]),
                (StartupPhase.SEMANTIC_INDEX_READY, qdrant_ok),
                (StartupPhase.RECOVERY_READY, recovery["ready"]),
                (StartupPhase.READ_MODEL_READY, read_model_ok),
            ]
            for phase, evidence in rung_evidence:
                if phase in ladder.reached:
                    continue
                expected = (
                    StartupPhase.BOOTSTRAP
                    if not ladder.reached
                    else ladder.next_phase()
                )
                if expected is not phase:
                    break
                try:
                    ladder.advance(phase, ready=bool(evidence))
                except StartupGateError as error:
                    ladder_fault = str(error)
                    break
            # CORE_READY: composite — every prior rung reached and the
            # startup gate itself passed (governance+security+authority+
            # audit+manifest evidence all green).
            if (
                not ladder_fault
                and ladder.next_phase() is StartupPhase.CORE_READY
            ):
                ladder.advance(StartupPhase.CORE_READY, ready=gate_ok)
            ladder_reached = [p.value for p in ladder.reached]
        except Exception as error:  # noqa: BLE001 — ladder is observability
            ladder_fault = f"{type(error).__name__}: {error}"

        if not gate_ok:
            startup_state = "FAILED"
        elif not qdrant_ok or not (ollama_ok or ollama_installed):
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
            "startup_ladder": {
                "reached": ladder_reached,
                "core_ready": ladder_reached[-1:] == ["CORE_READY"],
                "fault": ladder_fault,
                "evidence": ladder_evidence,
            },
            "phases": results,
        }
        return report


__all__ = ["StartupPhaseExecutionMixin"]
