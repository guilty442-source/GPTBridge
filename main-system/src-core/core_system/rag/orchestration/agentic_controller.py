"""Agentic Controller — adaptive orchestration, not a 4th retriever.

Agentic RAG = controlling multi-round retrieval.  The controller may
only invoke the formal retriever tools — never raw SQL, never direct
Qdrant, never arbitrary paths:

    retrieve_hybrid(query, scope)
    retrieve_code(query, scope)
    retrieve_memory(query, scope)
    retrieve_metadata(query, scope)

Each round: call tools -> fuse -> rerank -> sufficiency gate.
The gate (``evaluate_sufficiency``) decides RETRIEVE / FIND_AUTHORITY /
SUFFICIENT / STOP — not the model.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .evidence import RagArchitecture, RagEvidence
from .fusion import architecture_fusion, mark_conflicts
from .sufficiency import (
    SufficiencyPolicy,
    SufficiencyReport,
    SufficiencyVerdict,
    evaluate_sufficiency,
)

ALLOWED_TOOLS = (
    "retrieve_hybrid",
    "retrieve_code",
    "retrieve_memory",
    "retrieve_metadata",
)

# Tool name -> architecture whose pool the evidence joins.
_TOOL_ARCH = {
    "retrieve_hybrid": RagArchitecture.HYBRID,
    "retrieve_code": RagArchitecture.CODE,
    "retrieve_memory": RagArchitecture.MEMORY,
    "retrieve_metadata": RagArchitecture.HYBRID,
}

ToolFn = Callable[[str, dict[str, Any]], list[RagEvidence]]
ReformulatorFn = Callable[[str, list[RagEvidence], int], str]


@dataclass(frozen=True, slots=True)
class RoundTrace:
    round_number: int
    tool: str
    query: str
    evidence_count: int
    verdict: str


@dataclass(frozen=True, slots=True)
class AgenticResult:
    evidence: tuple[RagEvidence, ...]
    rounds: int
    traces: tuple[RoundTrace, ...]
    final_report: SufficiencyReport
    stopped_by: str     # "sufficient" | "budget" | "no-new-evidence" | "policy"


class AgenticController:
    """Bounded multi-round orchestrator over the formal retrievers."""

    def __init__(
        self,
        tools: dict[str, ToolFn],
        *,
        policy: SufficiencyPolicy = SufficiencyPolicy(),
        reformulator: Optional[ReformulatorFn] = None,
    ) -> None:
        unknown = set(tools) - set(ALLOWED_TOOLS)
        if unknown:
            raise ValueError(f"non-formal tools rejected: {sorted(unknown)}")
        self._tools = dict(tools)
        self._policy = policy
        self._reformulator = reformulator

    def call_tool(
        self, name: str, query: str, scope: dict[str, Any] | None = None
    ) -> list[RagEvidence]:
        """Dispatch one formal tool — allowlist enforced here too."""
        if name not in ALLOWED_TOOLS:
            raise PermissionError(f"tool '{name}' not in formal retriever set")
        fn = self._tools.get(name)
        if fn is None:
            return []
        return fn(query, scope or {})

    def run(
        self,
        query: str,
        *,
        tools_sequence: tuple[str, ...] = ("retrieve_hybrid",),
        required_aspects: tuple[str, ...] = (),
        scope: dict[str, Any] | None = None,
    ) -> AgenticResult:
        """Run the orchestration loop until the gate is satisfied or
        the round budget is spent."""
        pools: dict[RagArchitecture, list[RagEvidence]] = {}
        seen_ids: set[str] = set()
        traces: list[RoundTrace] = []
        current_query = query
        report = evaluate_sufficiency(
            [], policy=self._policy, required_aspects=required_aspects
        )
        stopped = "budget"

        for rnd in range(1, self._policy.max_rounds + 1):
            new = 0
            tool = tools_sequence[min(rnd - 1, len(tools_sequence) - 1)]
            try:
                results = self.call_tool(tool, current_query, scope)
            except PermissionError:
                results = []
            arch = _TOOL_ARCH.get(tool, RagArchitecture.HYBRID)
            for e in results:
                if e.evidence_id in seen_ids:
                    continue
                seen_ids.add(e.evidence_id)
                pools.setdefault(arch, []).append(e)
                new += 1

            fused = mark_conflicts(architecture_fusion(pools))
            report = evaluate_sufficiency(
                fused,
                policy=self._policy,
                required_aspects=required_aspects,
                round_number=rnd,
            )
            traces.append(RoundTrace(rnd, tool, current_query, len(fused), report.verdict.value))

            if report.verdict is SufficiencyVerdict.SUFFICIENT:
                stopped = "sufficient"
                break
            if rnd >= self._policy.max_rounds:
                break
            if new == 0 and rnd > 1:
                stopped = "no-new-evidence"
                break
            if self._reformulator is not None:
                try:
                    reformulated = self._reformulator(current_query, fused, rnd)
                except Exception:
                    break
                if reformulated and reformulated != current_query:
                    current_query = reformulated
                else:
                    stopped = "no-new-evidence"
                    break
            elif report.verdict is SufficiencyVerdict.RETRIEVE and new == 0:
                stopped = "no-new-evidence"
                break

        return AgenticResult(
            evidence=tuple(mark_conflicts(architecture_fusion(pools))),
            rounds=len(traces),
            traces=tuple(traces),
            final_report=report,
            stopped_by=stopped,
        )


__all__ = [
    "ALLOWED_TOOLS",
    "AgenticController",
    "AgenticResult",
    "RoundTrace",
    "ToolFn",
]
