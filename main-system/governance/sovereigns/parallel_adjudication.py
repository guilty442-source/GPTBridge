"""Sovereign Parallel Adjudication — parallel multi-core intent adjudication for sovereigns.

法典依據:
- CONCURRENCY: independent-domain-checks parallel-after-assignment +
  dependent-gates serial + result-join requires-all-mandatory-current proofs.
- A446: every tier receipted; the executor never self-declares success.
- A10/A11: fail-closed — one failed mandatory check denies the joined result.
- A69/A121: every run leaves a machine-readable receipt.

Provides a reusable parallel adjudication engine that sovereigns can use
to execute multiple independent adjudication paths concurrently.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence, TypeVar

from ..parallel_core import (
    CheckVerdict,
    DomainCheck,
    ParallelRun,
    SovereignParallelCore,
)

T = TypeVar("T")


@dataclass(frozen=True)
class AdjudicationTask:
    """One independent adjudication task for parallel execution."""

    name: str
    intent: str
    handler: Callable[[], Awaitable[Any]]
    mandatory: bool = True


@dataclass(frozen=True)
class AdjudicationResult:
    """Result of a parallel adjudication stage."""

    label: str
    ok: bool
    verdicts: tuple[CheckVerdict, ...]
    failed: tuple[str, ...]
    results: dict[str, Any] = field(default_factory=dict)

    def verdict(self, name: str) -> CheckVerdict | None:
        for item in self.verdicts:
            if item.name == name:
                return item
        return None

    def summary(self) -> dict[str, Any]:
        return {
            "core": self.label,
            "mode": "parallel-adjudication",
            "ok": self.ok,
            "failed": list(self.failed),
            "checks": [item.to_record() for item in self.verdicts],
            "results": self.results,
        }


class SovereignParallelAdjudicator:
    """Execute independent adjudication tasks concurrently, join fail-closed.

    Each task runs under an optional deadline. Exceptions/timeouts are recorded
    as failed verdicts. A failed mandatory task stops the dependent chain.
    """

    def __init__(
        self,
        *,
        label: str = "sovereign-adjudication",
        deadline: float | None = None,
    ) -> None:
        self._label = label
        self._deadline = deadline
        self._core = SovereignParallelCore(label=label, deadline=deadline)

    async def run_stage(self, tasks: Sequence[AdjudicationTask]) -> AdjudicationResult:
        """Run a stage of independent adjudication tasks in parallel."""
        checks = [
            DomainCheck(
                name=task.name,
                run=task.handler,
                mandatory=task.mandatory,
            )
            for task in tasks
        ]
        run = await self._core.run_stage(checks)

        results: dict[str, Any] = {}
        for task in tasks:
            verdict = run.verdict(task.name)
            if verdict and verdict.ok and not verdict.error:
                try:
                    result = task.handler()
                    if asyncio.iscoroutine(result):
                        result = await result
                    results[task.name] = result
                except Exception:
                    pass

        return AdjudicationResult(
            label=self._label,
            ok=run.ok,
            verdicts=run.verdicts,
            failed=run.failed,
            results=results,
        )

    async def run_stages(
        self, stages: Sequence[Sequence[AdjudicationTask]]
    ) -> tuple[AdjudicationResult, ...]:
        """Run multiple stages serially, stopping on mandatory failure."""
        runs: list[AdjudicationResult] = []
        for stage in stages:
            run = await self.run_stage(stage)
            runs.append(run)
            if not run.ok:
                break
        return tuple(runs)

    async def _execute_task(self, task: AdjudicationTask) -> CheckVerdict:
        """Execute a single adjudication task with timeout handling."""
        started = time.perf_counter()
        ok = False
        error = ""
        detail: dict[str, Any] = {}
        try:
            result = task.handler()
            if asyncio.iscoroutine(result):
                if self._deadline is not None:
                    result = await asyncio.wait_for(result, self._deadline)
                else:
                    result = await result
            if isinstance(result, bool):
                ok = result
            elif isinstance(result, Mapping):
                ok = bool(result.get("ok"))
                detail = dict(result)
            else:
                ok = bool(result)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            error = "TimeoutError"
        except Exception as exc:
            error = type(exc).__name__
        duration_ms = int((time.perf_counter() - started) * 1000)
        return CheckVerdict(
            name=task.name,
            ok=ok and not error,
            duration_ms=duration_ms,
            mandatory=task.mandatory,
            error=error,
            detail=detail,
        )


async def parallel_adjudicate(
    sovereign: Any,
    request: Any,
    intent_handlers: Mapping[str, Callable[[Any], Awaitable[Any]]],
    *,
    deadline: float = 5.0,
) -> dict[str, Any]:
    """Convenience function for parallel adjudication across multiple intents.

    Args:
        sovereign: The sovereign instance
        request: The sovereign request
        intent_handlers: Mapping of intent -> async handler function
        deadline: Per-task deadline in seconds

    Returns:
        Dict with adjudication results for each intent
    """
    adjudicator = SovereignParallelAdjudicator(
        label=f"{sovereign.sovereign_id}-adjudication", deadline=deadline
    )

    tasks = [
        AdjudicationTask(
            name=intent,
            intent=intent,
            handler=lambda i=intent: intent_handlers[i](request),
        )
        for intent in intent_handlers
    ]

    run = await adjudicator.run_stage(tasks)
    return {
        "ok": run.ok,
        "verdicts": [v.to_record() for v in run.verdicts],
        "results": run.results,
        "failed": list(run.failed),
    }


__all__ = [
    "AdjudicationTask",
    "AdjudicationResult",
    "SovereignParallelAdjudicator",
    "parallel_adjudicate",
]