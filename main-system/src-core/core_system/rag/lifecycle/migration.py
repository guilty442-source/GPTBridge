"""Governed schema-migration executor — the migration tool.

``plan_migration`` decides *what* must happen; ``apply_migration``
*runs* the phases in order. Vector-axis drift triggers a new-generation
build through the governed rebuild path (``rebuild_canonical``:
PostgreSQL authority -> rechunk/re-embed -> validate -> atomic alias
promote). The metadata axis needs an explicitly injected executor —
a plan without one is blocked before any mutation, never half-applied.

Drift-triggered new-generation builds are the P2/G50 residual: the
pipeline flags ``_generation_rebuild_required`` on fingerprint drift;
the governed path to resolve it is this executor, not a bare rebuild
call.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from .dr import RebuildStep
from .schema_versions import MigrationPhase, MigrationPlan


@dataclass(frozen=True, slots=True)
class MigrationReport:
    plan: MigrationPlan
    phases_completed: tuple[MigrationPhase, ...]
    ok: bool
    vector_generation_built: bool
    blocked_reason: str = ""


MigrationExecutor = Callable[[MigrationPlan], Any]


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _rebuild_complete(report: Any) -> bool:
    """A vector-generation build counts only when the governed rebuild
    report ran VALIDATE and ACTIVATE to completion."""
    if report is None:
        return False
    if getattr(report, "complete", False) is not True:
        return False
    steps = getattr(report, "steps_completed", ()) or ()
    return (
        RebuildStep.VALIDATE in steps and RebuildStep.ACTIVATE in steps
    )


def _blocked(
    plan: MigrationPlan,
    completed: list[MigrationPhase],
    reason: str,
) -> MigrationReport:
    return MigrationReport(
        plan=plan,
        phases_completed=tuple(completed),
        ok=False,
        vector_generation_built=False,
        blocked_reason=reason,
    )


async def apply_migration(
    plan: MigrationPlan,
    *,
    apply_metadata: Optional[MigrationExecutor] = None,
    build_vector_generation: Optional[MigrationExecutor] = None,
    validate: Optional[MigrationExecutor] = None,
) -> MigrationReport:
    """Execute ``plan`` phase-by-phase; fail-closed on a missing
    executor or a failing phase. ACTIVATE is only marked complete after
    every earlier phase succeeded — a blocked plan never activates."""
    completed: list[MigrationPhase] = [
        MigrationPhase.DETECT,
        MigrationPhase.PLAN,
    ]
    vector_built = False
    for phase in plan.phases:
        if phase in (MigrationPhase.DETECT, MigrationPhase.PLAN):
            continue
        if phase is MigrationPhase.APPLY_METADATA:
            if apply_metadata is None:
                return _blocked(
                    plan, completed, "metadata-migration-executor-missing"
                )
            try:
                outcome = await _maybe_await(apply_metadata(plan))
            except Exception as exc:  # fail-closed, auditable
                return _blocked(
                    plan, completed, f"metadata-migration-failed:{exc}"
                )
            if not outcome:
                return _blocked(plan, completed, "metadata-migration-failed")
        elif phase is MigrationPhase.BUILD_VECTOR_GENERATION:
            if build_vector_generation is None:
                return _blocked(
                    plan, completed, "vector-generation-builder-missing"
                )
            try:
                report = await _maybe_await(build_vector_generation(plan))
            except Exception as exc:
                return _blocked(
                    plan, completed, f"vector-generation-build-failed:{exc}"
                )
            if not _rebuild_complete(report):
                return _blocked(
                    plan, completed, "vector-generation-build-incomplete"
                )
            vector_built = True
        elif phase is MigrationPhase.VALIDATE:
            if validate is not None:
                try:
                    outcome = await _maybe_await(validate(plan))
                except Exception as exc:
                    return _blocked(
                        plan, completed, f"validation-failed:{exc}"
                    )
                if not outcome:
                    return _blocked(plan, completed, "validation-failed")
            # Without an extra validator the phase executors' own checks
            # stand (rebuild VALIDATE step / metadata executor result).
        elif phase is MigrationPhase.ACTIVATE:
            pass  # promotion happened inside the governed build/apply
        completed.append(phase)
    return MigrationReport(
        plan=plan,
        phases_completed=tuple(completed),
        ok=True,
        vector_generation_built=vector_built,
    )


__all__ = [
    "MigrationExecutor",
    "MigrationReport",
    "apply_migration",
]
