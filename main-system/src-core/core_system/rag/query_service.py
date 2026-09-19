"""RAG query service — canonical CAG→DAG→citation path wiring (A549).

A549 fixes the formal path Caller→Information Channel→Permission/Scope→
RagApplicationService→DAG Planner/Executor→CAG Gate→RAG Retrieval→…→
Citation Validation→Result.  This service is the application-service
facade: it consults the CAG plane first (gate-validated hit only), runs the
bounded query DAG on a miss, refuses uncited results and only then stores
the answer back into the cache.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .cag import CacheGateDecision, CacheRequest, CagCacheStore
from .dag import (
    RagDagExecutionContext,
    RagDagExecutionResult,
    RagDagExecutor,
    RagDagNodeType,
    RagDagPlanRequest,
    RagDagPlanner,
    RagDagState,
)
from .rag_contracts import Citation


@dataclass(frozen=True, slots=True)
class RagQueryOutcome:
    """Result of one canonical-path query."""

    ok: bool
    state: str
    answer_text: str = ""
    citations: tuple[Citation, ...] = ()
    cache_decision: CacheGateDecision | None = None
    dag_result: RagDagExecutionResult | None = None
    failure_reasons: tuple[str, ...] = ()

    def to_record(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "state": self.state,
            "answer_text": self.answer_text,
            "citations": [citation.citation_id for citation in self.citations],
            "cache_decision": self.cache_decision.to_record() if self.cache_decision else None,
            "dag_state": self.dag_result.state.value if self.dag_result else "",
            "evidence_digest": self.dag_result.evidence_digest if self.dag_result else "",
            "failure_reasons": list(self.failure_reasons),
        }


class RagQueryService:
    """CAG-first, DAG-backed, citation-validated query facade."""

    def __init__(
        self,
        *,
        planner: RagDagPlanner,
        executor: RagDagExecutor,
        store: CagCacheStore,
    ) -> None:
        self._planner = planner
        self._executor = executor
        self._store = store

    def query(
        self,
        *,
        cache_request: CacheRequest,
        plan_request: RagDagPlanRequest,
        context: RagDagExecutionContext,
        cancel_event: Any | None = None,
    ) -> RagQueryOutcome:
        entry, decision = self._store.get(cache_request)
        if entry is not None and decision.allowed:
            answer_text = str(entry.payload.get("answer_text") or "")
            citations = _citations_from_payload(entry.payload)
            if answer_text and citations:
                return RagQueryOutcome(
                    ok=True,
                    state="cache-hit",
                    answer_text=answer_text,
                    citations=citations,
                    cache_decision=decision,
                )

        plan = self._planner.plan(plan_request, context)
        result = self._executor.execute(plan, cancel_event=cancel_event)
        if result.state is not RagDagState.SUCCEEDED:
            return RagQueryOutcome(
                ok=False,
                state="dag-failed",
                cache_decision=decision,
                dag_result=result,
                failure_reasons=result.failure_reasons,
            )

        answer_text, citations = _extract_answer(result)
        if not answer_text or not citations:
            return RagQueryOutcome(
                ok=False,
                state="citation-failed",
                cache_decision=decision,
                dag_result=result,
                failure_reasons=("uncited-result",),
            )

        self._store.put(
            cache_request,
            {
                "answer_text": answer_text,
                "citations": [asdict(citation) for citation in citations],
            },
            generation_id=context.correlation_id,
        )
        return RagQueryOutcome(
            ok=True,
            state="dag-success",
            answer_text=answer_text,
            citations=citations,
            cache_decision=decision,
            dag_result=result,
        )


def _extract_answer(result: RagDagExecutionResult) -> tuple[str, tuple[Citation, ...]]:
    answer_text = ""
    citations: tuple[Citation, ...] = ()
    for node in result.node_results:
        if node.state is not RagDagState.SUCCEEDED:
            continue
        if node.node_type is RagDagNodeType.MODEL_INFERENCE:
            answer_text = str(node.evidence.get("answer_text") or "")
        elif node.node_type is RagDagNodeType.CITATION_VALIDATION:
            citations = _citations_from_evidence(node.evidence.get("validated_citations"))
    return answer_text, citations


def _citations_from_evidence(raw: Any) -> tuple[Citation, ...]:
    citations: list[Citation] = []
    for item in raw or ():
        if isinstance(item, Citation):
            citations.append(item)
        elif isinstance(item, Mapping):
            try:
                citations.append(Citation(**item))
            except TypeError:
                continue
    return tuple(citations)


def _citations_from_payload(payload: Mapping[str, Any]) -> tuple[Citation, ...]:
    return _citations_from_evidence(payload.get("citations"))


__all__ = ["RagQueryOutcome", "RagQueryService"]
