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
    (``startup_complete_deadline_ms``, codex: 10000 ms) with per-phase budgets
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
    governed_startup_constant as _cfg_gs,
)

from .governed_startup_types import  StartupGeneration
from .governed_startup_verify import (
    verify_core_ready,
    verify_phase_order,
)
from .startup_executor_types import PhaseRecord, StartupResult, _iso_now
from .startup_lifecycle_types import ReadinessProof
from .startup_lifecycle_verify import verify_readiness_handoff
from .startup_executor_phases import StartupExecutorPhasesMixin


# ---------------------------------------------------------------------------
# Startup Sovereign executor
# ---------------------------------------------------------------------------

class StartupSovereignExecutor(StartupExecutorPhasesMixin):
    """Executes the certified startup phase sequence for one generation.

    Single-flight (E155/P95): a second ``run()`` while a generation is
    active is rejected — startup is owned by exactly one generation at a
    time.  Each phase is bounded by ``asyncio.wait_for`` against the
    smaller of its declared phase budget and the remaining generation
    budget; a required phase failing or timing out aborts the generation
    and runs reverse-order cleanup of already-activated nodes before the
    result is published through the information layer.
    """

    ROLE = "startup-sub-sovereign"

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

    async def run(
        self,
        generation_id: str | None = None,
        *,
        deadline_epoch: float | None = None,
    ) -> StartupResult:
        if self._lock.locked():
            # E155/P95 single-flight: concurrent startup generations are
            # forbidden — the existing generation owns the sequence.
            return StartupResult(
                generation_id="",
                ok=False,
                violations=["startup-generation-in-flight"],
            )
        async with self._lock:
            return await self._run_generation(generation_id, deadline_epoch=deadline_epoch)

    async def _run_generation(
        self,
        generation_id: str | None = None,
        *,
        deadline_epoch: float | None = None,
    ) -> StartupResult:
        phases = tuple(str(p) for p in _cfg_gs("startup_phases"))
        budgets = dict(_cfg_gs("phase_budget_ms") or {})
        deadline_ms = int(_cfg_gs("startup_complete_deadline_ms") or 10000)
        # Use generation_id from boot_core if provided, otherwise generate new
        if not generation_id:
            generation_id = uuid.uuid4().hex
        release_id = str(getattr(self.app, "version", "") or "unknown")
        # P110/E173: the deadline is a single monotonic clock for the
        # complete startup.  When the caller supplies the generation epoch
        # (app construction / sequence entry), pre-executor work consumes
        # the same 10 s budget — never a second, hidden clock.
        started = deadline_epoch if deadline_epoch is not None else time.monotonic()

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
            "flow": "startup-sub-sovereign>information-layer>runtime-sovereign",
            "proof": proof.as_dict(),
            "acknowledged": True,
            "runtime_owner": "system-runtime-sovereign",
        }
        self._conditions["handoff-acknowledged"] = True
        self._mark_phase("handoff-complete")
        self._publish_state(result)
        return result

    # ------------------------------------------------------------------
    # Handoff proof (A194/E169)
    # ------------------------------------------------------------------

    def _build_readiness_proof(
        self, generation_id: str, release_id: str
    ) -> ReadinessProof:
        app = self.app
        try:
            # A435 bounded lookup: codex identity tuple only, through the
            # official entry (never a direct repository read).
            from governance_rule.execution.codex_reconcile import (
                bounded_lookup,
            )

            identity = bounded_lookup(
                "startup-executor",
                purpose="status",
                scope=("codex:identity",),
                reader=lambda ctx: ctx.codex_identity(),
            )
            codex_identity = (
                f"{identity['schema']}:{identity['codex_version_text']}"
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
