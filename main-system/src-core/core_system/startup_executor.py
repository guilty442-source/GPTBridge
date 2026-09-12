"""Startup Sovereign executor — certified-manifest-driven startup DAG.

Implements the startup provisions of the Governance Codex:

  * A191/E166 — dependencies are classified by the *current certified
    manifest* declaration (identity+owner+required-by+criticality+
    readiness-contract+deadline+retry-budget+shutdown-order), never by
    hardcoded name or legacy fixed sequence.
  * A192/E167 — the startup sovereign holds exactly three capabilities:
    bootstrap-and-authority-readiness-orchestration, certified-DAG-and-
    sovereign-activation, readiness-handoff-or-owned-failure-rollback.
  * E155 — declared DAG + cycle-reject + verified nodes + bounded
    independent parallelism + atomic generation-fenced activation;
    REQUIRED-FAIL aborts with reverse-order cleanup, OPTIONAL-FAIL
    degrades that capability only; status is published through the
    information layer (state file); partial-ready is forbidden.
  * A194/E169 — core-ready produces a ReadinessProof that is handed off
    to the system-runtime sovereign; after handoff the startup sovereign
    holds no runtime control.
  * P110/E173 — the complete startup has a single monotonic deadline
    (``startup_deadline_ms``, codex: 10000 ms) with per-phase budgets
    (``phase_budget_ms``); exceeding the deadline fails the generation
    closed, reports the exact bottleneck, and never reports false-ready.

The executor runs the certified ``governed_startup.startup_phases`` in
declared order (A192 phase ordering), each phase bounded by
``asyncio.wait_for`` against its declared budget AND the remaining
generation budget.  All work delegated here stays decision/orchestration
level — heavy execution remains with governed executors.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

from startup_core.startup_config import (
    dependency_manifest as _cfg_dependency_manifest,
)
from startup_core.startup_config import (
    governed_startup_constant as _cfg_gs,
)

from .governed_startup_types import DependencyDeclaration, StartupGeneration
from .governed_startup_verify import (
    DependencyDAG,
    verify_core_ready,
    verify_dependency_classification,
    verify_phase_order,
)
from .startup_executor_types import PhaseRecord, StartupResult, _iso_now
from .startup_lifecycle_types import ReadinessProof
from .startup_lifecycle_verify import verify_readiness_handoff


# ---------------------------------------------------------------------------
# Startup Sovereign executor
# ---------------------------------------------------------------------------

class StartupSovereignExecutor:
    """Executes the certified startup phase sequence for one generation.

    Single-flight (E155/P95): a second ``run()`` while a generation is
    active is rejected — startup is owned by exactly one generation at a
    time.  Each phase is bounded by ``asyncio.wait_for`` against the
    smaller of its declared phase budget and the remaining generation
    budget; a required phase failing or timing out aborts the generation
    and runs reverse-order cleanup of already-activated nodes before the
    result is published through the information layer.
    """

    ROLE = "startup-sovereign"

    def __init__(self, app: Any) -> None:
        self.app = app
        self._lock = asyncio.Lock()
        self._generation: StartupGeneration | None = None
        self._activated: list[tuple[str, Callable[[], Awaitable[None]]]] = []
        self._conditions: dict[str, bool] = {}
        self._state_path = (
            Path(getattr(app, "project_root", ".")).resolve()
            / "main-system" / "runtime" / "state" / "startup-generation.json"
        )

    # ------------------------------------------------------------------
    # Phase registry — manifest phase names -> executor handlers
    # ------------------------------------------------------------------

    def _handlers(self) -> dict[str, Callable[[PhaseRecord], Awaitable[None]]]:
        return {
            "phase-0-local-preflight": self._phase_local_preflight,
            "phase-1-minimal-information-bootstrap": (
                self._phase_minimal_information_bootstrap
            ),
            "phase-2-read-official-codex": self._phase_read_official_codex,
            "phase-3-load-permission-directory": (
                self._phase_load_permission_directory
            ),
            "phase-4-switch-normal-information-mode": (
                self._phase_switch_normal_information_mode
            ),
            "phase-5-classify-dependency-dag": self._phase_classify_dependency_dag,
            "phase-6-activate-core-sovereigns": self._phase_activate_core_sovereigns,
        }

    # ------------------------------------------------------------------
    # Public entry — single-flight, generation-fenced
    # ------------------------------------------------------------------

    async def run(self, generation_id: str | None = None) -> StartupResult:
        if self._lock.locked():
            # E155/P95 single-flight: concurrent startup generations are
            # forbidden — the existing generation owns the sequence.
            return StartupResult(
                generation_id="",
                ok=False,
                violations=["startup-generation-in-flight"],
            )
        async with self._lock:
            return await self._run_generation(generation_id)

    async def _run_generation(self, generation_id: str | None = None) -> StartupResult:
        phases = tuple(str(p) for p in _cfg_gs("startup_phases"))
        budgets = dict(_cfg_gs("phase_budget_ms") or {})
        deadline_ms = int(_cfg_gs("startup_deadline_ms") or 10000)
        # Use generation_id from boot_core if provided, otherwise generate new
        if not generation_id:
            generation_id = uuid.uuid4().hex
        release_id = str(getattr(self.app, "version", "") or "unknown")
        started = time.monotonic()

        self._generation = StartupGeneration(
            generation_id=generation_id,
            release_id=release_id,
            started_at=_iso_now(),
            current_phase="phase-0-local-preflight",
            core_ready=False,
            deferred_active=False,
        )
        self._activated = []
        self._conditions = {}

        result = StartupResult(
            generation_id=generation_id,
            ok=False,
            deadline_ms=deadline_ms,
        )
        handlers = self._handlers()

        for phase_id in phases:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            remaining_ms = deadline_ms - elapsed_ms
            budget_ms = min(int(budgets.get(phase_id, remaining_ms)), remaining_ms)
            record = PhaseRecord(phase_id=phase_id, budget_ms=budget_ms)
            result.phases.append(record)

            if remaining_ms <= 0:
                # P110/E173: deadline exceeded — fail the generation closed.
                record.error = "startup-deadline-exceeded"
                result.failure_phase = phase_id
                result.violations.append("deadline-exceeded")
                break

            self._mark_phase(phase_id)
            handler = handlers.get(phase_id)
            if handler is None:
                # Unregistered declared phase — fail closed, never skip.
                record.error = "undeclared-phase-handler"
                result.failure_phase = phase_id
                result.violations.append(f"undeclared-phase:{phase_id}")
                break

            try:
                await asyncio.wait_for(handler(record), timeout=budget_ms / 1000)
                record.ok = True
            except asyncio.TimeoutError:
                record.error = f"phase-deadline-exceeded:{budget_ms}ms"
                result.failure_phase = phase_id
                result.violations.append(f"phase-timeout:{phase_id}")
                break
            except Exception as error:
                record.error = f"{type(error).__name__}: {error}"
                result.failure_phase = phase_id
                break
            finally:
                record.duration_ms = int((time.monotonic() - started) * 1000) - (
                    elapsed_ms
                )

        result.elapsed_ms = int((time.monotonic() - started) * 1000)
        result.bottleneck = self._bottleneck(result.phases)

        order_check = verify_phase_order(
            tuple(p.phase_id for p in result.phases if p.ok)
        )
        if not order_check["ok"]:
            result.violations.extend(order_check["violations"])

        if result.failure_phase or result.violations:
            # E155 REQUIRED-FAIL: abort + reverse-DAG cleanup of activated
            # nodes, then fail the generation closed — no partial-ready.
            await self._reverse_cleanup()
            self._generation = None
            self._publish_state(result)
            return result

        # A194/E169: core-ready produces a proof-bound handoff to the
        # system-runtime sovereign; startup relinquishes runtime control.
        ready = verify_core_ready(self._conditions)
        if not ready["ok"]:
            result.violations.extend(
                f"missing-ready-condition:{c}" for c in ready["missing"]
            )
            await self._reverse_cleanup()
            self._generation = None
            self._publish_state(result)
            return result

        proof = self._build_readiness_proof(generation_id, release_id)
        handoff_check = verify_readiness_handoff(proof, acknowledged=True)
        if not handoff_check.ok:
            result.violations.extend(handoff_check.violations)
            await self._reverse_cleanup()
            self._generation = None
            self._publish_state(result)
            return result

        result.ok = True
        result.handoff = {
            "flow": "startup-sovereign>information-layer>system-runtime-sovereign",
            "proof": proof.as_dict(),
            "acknowledged": True,
            "runtime_owner": "system-runtime-sovereign",
        }
        self._conditions["handoff-acknowledged"] = True
        self._mark_phase("handoff-complete")
        self._publish_state(result)
        return result

    # ------------------------------------------------------------------
    # Phase handlers — decision/orchestration level only
    # ------------------------------------------------------------------

    async def _phase_local_preflight(self, record: PhaseRecord) -> None:
        """PHASE-0: local preflight — canonical root and sealed layout only."""
        app = self.app
        root = Path(getattr(app, "project_root", "")).resolve()
        required = (
            root / "governance_rule",
            root / "governance_rule" / "codex" / "data" / "governance_codex.sqlite3",
            root / "main-system" / "src-core" / "main.py",
            root / "shared-layer" / "src",
        )
        missing = [str(p) for p in required if not p.exists()]
        if missing:
            raise RuntimeError(f"preflight-missing:{','.join(missing)}")
        record.detail["project_root"] = str(root)
        self._conditions["local-preflight-ok"] = True

    async def _phase_minimal_information_bootstrap(
        self, record: PhaseRecord
    ) -> None:
        """PHASE-1: minimal information bootstrap (last-valid-sealed state)."""
        app = self.app
        if app.task_queue is None:
            from tasks.queue import TaskQueue

            app.task_queue = TaskQueue(app.project_root, app.core_logger)
        # A67: repair coordinator prevents duplicate repair owners.
        from tasks.repair_coordinator import init_repair_coordinator

        init_repair_coordinator(app.project_root)
        self._conditions["minimal-information-bootstrap"] = True

    async def _phase_read_official_codex(self, record: PhaseRecord) -> None:
        """PHASE-2: read the official codex and verify runtime integrity."""
        app = self.app
        if app.governance is None:
            from core_system.governance_runtime import MainSystemGovernance

            app.governance = MainSystemGovernance.from_environment(app.project_root)
        from governance_rule.execution.codex_repository import load_governance_codex

        codex = await asyncio.to_thread(load_governance_codex)
        integrity = False
        try:
            integrity = bool(app.governance.runtime_integrity_ready())
        except Exception:
            integrity = False
        if not integrity:
            raise RuntimeError("official-codex-integrity-unverified")
        record.detail["codex_version"] = codex.codex_version
        self._conditions["official-codex-valid"] = True

    async def _phase_load_permission_directory(self, record: PhaseRecord) -> None:
        """PHASE-3: load the permission directory; activate permission sovereign."""
        app = self.app
        from governance_rule.permission_directory.code_rule_directory import (
            code_rule_directory_snapshot,
        )

        directory = code_rule_directory_snapshot()
        if not directory.approved_tool_ids:
            raise RuntimeError("permission-directory-empty")
        if app.permission_sovereign is None:
            from core_system.permission_sovereign import PermissionSovereign

            app.permission_sovereign = PermissionSovereign(
                app, governance=app.governance
            )
        record.detail["approved_tools"] = len(directory.approved_tool_ids)
        self._conditions["permission-sovereign-active"] = True

    async def _phase_switch_normal_information_mode(
        self, record: PhaseRecord
    ) -> None:
        """PHASE-4: normal information mode — channel + toolbox + status."""
        app = self.app
        if app.toolbox_service is None:
            from tasks.toolbox_service import ToolboxService

            app.toolbox_service = ToolboxService(
                app.project_root,
                governance=app.governance,
                permission_sovereign=app.permission_sovereign,
            )
        if app.runtime_status_service is None:
            from tasks.runtime_status_service import RuntimeStatusService

            app.runtime_status_service = RuntimeStatusService(app)
        if getattr(app, "command_router", None) is None:
            await app.runtime_bootstrap.initialize_main()
        # The state-change notifier (created when the listener bound) is the
        # normal-mode information channel; absence means the listener never
        # came up — fail closed rather than proceed deaf.
        if getattr(app, "_state_change_notifier", None) is None:
            raise RuntimeError("normal-information-layer-unavailable")
        self._conditions["normal-information-layer-active"] = True

    async def _phase_classify_dependency_dag(self, record: PhaseRecord) -> None:
        """PHASE-5: build + verify the certified dependency DAG (E155/A191)."""
        declarations = tuple(
            DependencyDeclaration(**entry) for entry in _cfg_dependency_manifest()
        )
        dag = DependencyDAG(dependencies=declarations)
        if not dag.is_acyclic:
            raise RuntimeError("dependency-dag-cycle-rejected")
        check = verify_dependency_classification(dag)
        if not check["ok"]:
            raise RuntimeError(
                "dependency-classification-violation:"
                + ",".join(check["violations"])
            )
        # Core-critical dependencies must be reachable for core-ready;
        # capability-critical failures degrade their owner capability only.
        from shared_layer.service_probe import probe_registered_local_service

        dependency_evidence: dict[str, Any] = {}
        core_ready = True
        for dep in dag.dependencies:
            probe = await asyncio.to_thread(
                probe_registered_local_service, dep.identity, timeout=0.75
            )
            dependency_evidence[dep.identity] = {
                "criticality": dep.criticality,
                "reachable": probe.reachable,
                "readiness_contract": dep.readiness_contract,
            }
            if dep.is_core_critical and not probe.reachable:
                core_ready = False
        if not core_ready:
            unreachable = [
                name
                for name, ev in dependency_evidence.items()
                if ev["criticality"] == "core-critical" and not ev["reachable"]
            ]
            raise RuntimeError(
                "core-critical-dependency-unreachable:" + ",".join(unreachable)
            )
        record.detail["dependencies"] = dependency_evidence
        record.detail["degraded_capabilities"] = [
            dep.required_by
            for dep in dag.dependencies
            if dep.is_capability_critical
            and not dependency_evidence[dep.identity]["reachable"]
        ]
        self._dependency_evidence = dependency_evidence
        self._conditions["all-core-critical-dependencies-ready"] = core_ready

    async def _phase_activate_core_sovereigns(self, record: PhaseRecord) -> None:
        """PHASE-6: activate core sovereigns with bounded parallelism (E155)."""
        app = self.app
        started = await app.system_sovereign_service.start_sovereign_stack()
        if not started:
            raise RuntimeError("sovereign-activation-failed")
        self._activated.append(
            ("sovereign-stack", app.system_sovereign_service.stop)
        )
        self._conditions.update(
            {
                "system-decision-active": True,
                "system-runtime-active": True,
                "maintenance-active": bool(
                    getattr(app, "maintenance_ready", False)
                ),
            }
        )

    # ------------------------------------------------------------------
    # Handoff proof (A194/E169)
    # ------------------------------------------------------------------

    def _build_readiness_proof(
        self, generation_id: str, release_id: str
    ) -> ReadinessProof:
        app = self.app
        try:
            from governance_rule.execution.codex_repository import (
                format_codex_version,
                load_governance_codex,
            )

            codex_identity = (
                f"{load_governance_codex().schema}:"
                f"{format_codex_version(load_governance_codex().codex_version)}"
            )
        except Exception:
            codex_identity = "unknown"
        material = json.dumps(
            {
                "generation": generation_id,
                "release": release_id,
                "codex": codex_identity,
                "conditions": self._conditions,
            },
            sort_keys=True,
            ensure_ascii=False,
        ).encode("utf-8")
        return ReadinessProof(
            startup_generation=generation_id,
            release_id=release_id,
            codex_identity=codex_identity,
            permission_directory_version=str(
                getattr(app, "version", "") or "unknown"
            ),
            information_layer_generation=generation_id,
            activated_node_set=tuple(name for name, _ in self._activated),
            dependency_evidence=getattr(self, "_dependency_evidence", {}),
            resource_allocation={
                "backend_pid": os.getpid(),
                "generation": generation_id,
            },
            health_baseline={"conditions": dict(self._conditions)},
            timestamp=_iso_now(),
            expiry=_iso_now(),
            content_hash=hashlib.sha256(material).hexdigest(),
        )

    # ------------------------------------------------------------------
    # Cleanup / reporting
    # ------------------------------------------------------------------

    async def _reverse_cleanup(self) -> None:
        """E155 REQUIRED-FAIL: reverse-order deactivation of started nodes."""
        for name, stop in reversed(self._activated):
            try:
                await stop()
            except Exception:
                pass
        self._activated.clear()
        self.app.startup_dead = True

    def _bottleneck(self, phases: list[PhaseRecord]) -> str:
        if not phases:
            return ""
        slowest = max(phases, key=lambda p: p.duration_ms)
        return f"{slowest.phase_id}:{slowest.duration_ms}ms"

    def _mark_phase(self, phase: str) -> None:
        mark = getattr(self.app, "_mark_startup_phase", None)
        if callable(mark):
            try:
                mark(phase)
            except Exception:
                pass

    def _publish_state(self, result: StartupResult) -> None:
        """E155: startup status goes through the information layer."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(
                    {
                        "version": "1.0.0",
                        "role": self.ROLE,
                        "result": result.as_dict(),
                        "updated_at": _iso_now(),
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._state_path)
        except OSError:
            pass


__all__ = [
    "PhaseRecord",
    "StartupResult",
    "StartupSovereignExecutor",
]
