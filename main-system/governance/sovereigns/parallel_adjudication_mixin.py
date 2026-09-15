"""Parallel Adjudication Mixin — reusable parallel adjudication for sovereigns.

法典依據:
- CONCURRENCY: independent-domain-checks parallel-after-assignment +
  dependent-gates serial + result-join requires-all-mandatory-current proofs.
- A446: every tier receipted; the executor never self-declares success.
- A10/A11: fail-closed — one failed mandatory check denies the joined result.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Mapping

from .parallel_adjudication import (
    AdjudicationResult,
    AdjudicationTask,
    SovereignParallelAdjudicator,
    parallel_adjudicate,
)


class ParallelAdjudicationMixin:
    """Mixin providing parallel adjudication capabilities for sovereigns."""

    def _create_parallel_adjudicator(
        self,
        *,
        label: str | None = None,
        deadline: float | None = None,
    ) -> SovereignParallelAdjudicator:
        """Create a parallel adjudicator for this sovereign."""
        return SovereignParallelAdjudicator(
            label=label or f"{self.sovereign_id}-parallel",
            deadline=deadline,
        )

    async def _run_parallel_adjudication(
        self,
        tasks: list[AdjudicationTask],
        *,
        deadline: float | None = None,
        label: str | None = None,
    ) -> AdjudicationResult:
        """Run a single stage of parallel adjudication tasks."""
        adjudicator = self._create_parallel_adjudicator(
            label=label or f"{self.sovereign_id}-adjudication",
            deadline=deadline,
        )
        return await adjudicator.run_stage(tasks)

    async def _run_parallel_adjudication_stages(
        self,
        stages: list[list[AdjudicationTask]],
        *,
        deadline: float | None = None,
        label: str | None = None,
    ) -> list[AdjudicationResult]:
        """Run multiple stages of parallel adjudication serially."""
        adjudicator = self._create_parallel_adjudicator(
            label=label or f"{self.sovereign_id}-adjudication",
            deadline=deadline,
        )
        runs = await adjudicator.run_stages(stages)
        return list(runs)

    async def _adjudicate_parallel(
        self,
        request: Any,
        intent_handlers: Mapping[str, Callable[[Any], Awaitable[Any]]],
        *,
        deadline: float = 5.0,
    ) -> dict[str, Any]:
        """Adjudicate multiple intents in parallel and return combined result.

        This is a convenience method for sovereigns that need to handle
        multiple independent intents concurrently.
        """
        return await parallel_adjudicate(
            sovereign=self,
            request=request,
            intent_handlers=intent_handlers,
            deadline=deadline,
        )


__all__ = ["ParallelAdjudicationMixin"]