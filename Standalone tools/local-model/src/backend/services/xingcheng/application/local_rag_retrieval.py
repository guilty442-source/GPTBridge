from __future__ import annotations

import re
from typing import Any

from shared_layer.adaptive.bounded_executor import (
    AdmissionRejected,
    OverflowPolicy,
    PoolPaused,
    PoolPolicy,
    WorkExpired,
    pool_for,
)
from shared_layer.adaptive.types import PriorityClass
from shared_layer.resource_identity import (
    XINGCHENG_MODULE_ID,
    canonical_identifier,
)

# A116 envelope declaration: bounds are module-declared; the effective
# worker count is the governor's "rag" class quota (concurrency-budget/v1).
_RAG_RETRIEVAL_POLICY = PoolPolicy(
    pool="rag.retrieval",
    work_class="rag",
    min_workers=1,
    max_workers=8,
    queue_capacity=128,
    deadline_ms=30_000,
    overflow=OverflowPolicy.REJECT,
    backpressure_wait_ms=2_000,
)


def _rag_admission_error(exc: BaseException) -> dict[str, Any]:
    """Map bounded-queue admission failures onto the service error contract."""
    code = (
        "RAG_POOL_PAUSED"
        if isinstance(exc, PoolPaused)
        else "RAG_DEADLINE_EXCEEDED"
        if isinstance(exc, WorkExpired)
        else "RAG_ADMISSION_REJECTED"
    )
    return {
        "ok": False,
        "error_code": code,
        "message": "檢索佇列已滿或暫停，請稍後再試。",
        "citations": [],
        "retrieved_count": 0,
        "reconciliation_required": False,
    }


class LocalRagRetrievalMixin:
    """Bounded admission, hybrid retrieval, and routing for LocalRagService.

    Answer composition (``_generate``/``_answer``/``status`` …) lives in
    :mod:`local_rag_answer` — A185 keeps each module under its bound.
    """

    @staticmethod
    def _hybrid_rrf(
        vector_results: list[dict[str, Any]], keyword_results: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        fused: dict[str, dict[str, Any]] = {}
        for channel, records in (("vector", vector_results), ("keyword", keyword_results)):
            for rank, item in enumerate(records, start=1):
                chunk_id = str(item.get("chunk_id") or "")
                if not chunk_id:
                    continue
                record = fused.setdefault(chunk_id, dict(item))
                for key, value in item.items():
                    record.setdefault(key, value)
                record["rrf_score"] = float(record.get("rrf_score") or 0.0) + 1.0 / (60 + rank)
                record[f"{channel}_rank"] = rank
        ranked = list(fused.values())
        ranked.sort(key=lambda item: -float(item.get("rrf_score") or 0.0))
        return ranked

    @classmethod
    def _deterministic_route(cls, question: str, payload: dict[str, Any]) -> str:
        requested = str(payload.get("rag_mode") or "").strip().casefold()
        if requested in cls.RAG_MODELS:
            return requested
        if payload.get("images") or re.search(r"圖片|影像|截圖|照片|image|visual", question, re.I):
            return "visual"
        if re.search(r"程式|程式碼|函式|除錯|code|debug|python|typescript|javascript|julia|sql", question, re.I):
            return "code"
        if re.search(r"深入|嚴謹推理|證明|多步推理|deep reasoning|prove", question, re.I):
            return "deep"
        if re.search(r"快速|簡短|一句|fast|brief", question, re.I):
            return "fast"
        return "general"

    def _route(self, question: str, payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        # 原生單模型架構下路由只決定檢索模式（不回應模型選擇），
        # 採確定性規則，避免為分類付出整次生成成本。
        route = self._deterministic_route(question, payload)
        reason = (
            "explicit-rag-mode"
            if str(payload.get("rag_mode") or "").casefold() in self.RAG_MODELS
            else "deterministic-routing"
        )
        return route, {
            "model": self.ROUTER_MODEL,
            "used": False,
            "reason": reason,
            "selected_route": route,
        }

    def _retrieve(
        self,
        vector: list[float],
        question: str,
        module_ids: tuple[str, ...],
        candidate_limit: int,
        canonical_ready: bool,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """A371/A373: canonical path first; bounded local mirror on failure."""
        if canonical_ready:
            try:
                parallel = getattr(
                    self.canonical, "query_vector_and_keyword", None
                )
                if callable(parallel):
                    # Dense + keyword round trips overlap on the pipeline
                    # loop — wall time is max(dense, keyword), not the sum.
                    return parallel(
                        vector,
                        question,
                        module_ids=module_ids,
                        limit=candidate_limit,
                    )
                return (
                    self.canonical.query_vector(
                        vector, module_ids=module_ids, limit=candidate_limit
                    ),
                    self.canonical.keyword_search(
                        question, module_ids=module_ids, limit=candidate_limit
                    ),
                )
            except (OSError, RuntimeError, ValueError) as exc:
                self.canonical.mark_unhealthy(str(exc))
        return (
            self.vector_store.query(vector, limit=candidate_limit, module_ids=module_ids),
            self.repository.keyword_search(
                question, limit=candidate_limit, module_ids=module_ids
            ),
        )

    def _retrieve_ranked(
        self,
        question: str,
        module_ids: tuple[str, ...],
        candidate_limit: int,
        canonical_ready: bool,
    ) -> tuple[list[dict[str, Any]], bool]:
        """Embed + canonical/degraded retrieval + RRF; returns (hybrid, pending)."""
        vectors = self._embed([question])
        vector_results, keyword_results = self._retrieve(
            vectors[0], question, module_ids, candidate_limit, canonical_ready
        )
        hybrid = self._hybrid_rrf(vector_results, keyword_results)
        if canonical_ready and not hybrid:
            # Canonical takeover proved live but holds no data yet; serve the
            # unreconciled local mirror once and flag it (A44 degraded read).
            return self._degraded_rrf(
                vectors[0], question, module_ids, candidate_limit
            )
        return hybrid, False

    def _degraded_rrf(
        self,
        vector: list[float],
        question: str,
        module_ids: tuple[str, ...],
        candidate_limit: int,
    ) -> tuple[list[dict[str, Any]], bool]:
        degraded_vector = self.vector_store.query(
            vector, limit=candidate_limit, module_ids=module_ids
        )
        degraded_keyword = self.repository.keyword_search(
            question, limit=candidate_limit, module_ids=module_ids
        )
        hybrid = self._hybrid_rrf(degraded_vector, degraded_keyword)
        return hybrid, bool(hybrid)

    @staticmethod
    def _module_scope(payload: dict[str, Any]) -> tuple[str, ...]:
        raw_scope = payload.get("_governed_module_ids")
        if isinstance(raw_scope, (list, tuple)) and raw_scope:
            return tuple(
                canonical_identifier(str(value), field="module_id")
                for value in raw_scope
            )
        return (XINGCHENG_MODULE_ID,)

    def query(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Bounded admission: queued onto the governor-sized rag pool."""
        executor = pool_for(_RAG_RETRIEVAL_POLICY)
        future = executor.submit(
            self._query_governed,
            payload,
            priority=PriorityClass.INTERACTIVE,
            wait_ms=_RAG_RETRIEVAL_POLICY.backpressure_wait_ms,
        )
        try:
            return future.result(
                timeout=_RAG_RETRIEVAL_POLICY.deadline_ms / 1000.0 + 5.0
            )
        except (AdmissionRejected, PoolPaused, WorkExpired) as exc:
            return _rag_admission_error(exc)

    def _query_governed(self, payload: dict[str, Any]) -> dict[str, Any]:
        module_ids = self._module_scope(payload)
        question = str(payload.get("question") or payload.get("prompt") or "").strip()
        if not question:
            return {"ok": False, "error_code": "RAG_QUESTION_REQUIRED", "message": "請提供 question 或 prompt。"}
        canonical_ready = self.canonical is not None and self.canonical.is_ready()
        candidate_limit = max(8, min(48, int(payload.get("candidate_limit") or 24)))
        try:
            ranked = self._retrieve_ranked(
                question, module_ids, candidate_limit, canonical_ready
            )
        except (OSError, RuntimeError, ValueError) as exc:
            return self._dependency_error(exc)
        matches, pending_reconciliation = ranked
        route, router = self._route(question, payload)
        reranked, reranker = self.reranker.rerank(
            question, matches[:candidate_limit], size="0.6b"
        )
        top_k = max(1, min(12, int(payload.get("top_k") or 6)))
        matches = reranked[:top_k]
        citations = self._citations(matches)
        if not matches:
            return self._empty_answer(
                route, router, reranker, canonical_ready and not pending_reconciliation
            )
        if payload.get("generate") is False:
            canonical = canonical_ready and not pending_reconciliation
            return {
                "ok": True, "answer": "", "response": "", "evidence_sufficient": True,
                "citations": citations, "retrieved_count": len(citations),
                "route": route, "router": router, "reranker": reranker,
                "reranker_fallback": reranker.get("fallback"),
                "generation_skipped": True,
                "retrieval": (
                    "canonical-vector-dense+postgresql-fts+index-state+rrf"
                    if canonical
                    else "local-vector-degraded-cache+postgresql-keyword+rrf"
                ),
                "canonical": canonical,
                "canonical_pending_reconciliation": pending_reconciliation,
                "reconciliation_required": not canonical,
                "authority": (
                    "canonical-vector-postgresql"
                    if canonical
                    else "non-canonical-reconciliation-required"
                ),
                "knowledge_base": "shared", "network_used": False,
            }
        return self._answer(
            question=question, matches=matches, citations=citations,
            route=route, router=router, reranker=reranker, payload=payload,
            canonical_ready=canonical_ready and not pending_reconciliation,
            pending_reconciliation=pending_reconciliation,
        )

    def infer_context(
        self,
        question: str,
        *,
        top_k: int = 4,
        candidate_limit: int = 12,
    ) -> dict[str, Any] | None:
        """Bounded read-only retrieval for inference grounding.

        Never blocks on canonical startup: the canonical path is consulted
        only while already ready (peeked), otherwise the bounded local
        mirror serves — matching the degraded-cache role. Skips the LLM
        router and answer generation; returns citations only. Any
        retrieval failure returns None so inference is never blocked.

        Admission is bounded through the governor-sized ``rag`` pool
        (concurrency-budget/v1): a full queue or paused class degrades to
        None instead of spawning unbounded retrieval contexts.
        """
        question = str(question or "").strip()
        if not question:
            return None
        executor = pool_for(_RAG_RETRIEVAL_POLICY)
        future = executor.submit(
            self._infer_context_governed,
            question,
            top_k=top_k,
            candidate_limit=candidate_limit,
            priority=PriorityClass.INTERACTIVE,
            wait_ms=1_000,
        )
        try:
            return future.result(
                timeout=_RAG_RETRIEVAL_POLICY.deadline_ms / 1000.0 + 5.0
            )
        except BaseException:
            return None

    def _infer_context_governed(
        self,
        question: str,
        *,
        top_k: int = 4,
        candidate_limit: int = 12,
    ) -> dict[str, Any] | None:
        question = str(question or "").strip()
        if not question:
            return None
        canonical_ready = (
            self.canonical is not None and self.canonical.peek_ready()
        )
        try:
            matches, pending_reconciliation = self._retrieve_ranked(
                question,
                (XINGCHENG_MODULE_ID,),
                max(8, min(48, int(candidate_limit))),
                canonical_ready,
            )
        except (OSError, RuntimeError, ValueError):
            return None
        if not matches:
            return None
        reranked, reranker = self.reranker.rerank(
            question, matches[:candidate_limit], size="0.6b"
        )
        citations = self._citations(reranked[: max(1, min(12, int(top_k)))])
        if not citations:
            return None
        canonical = canonical_ready and not pending_reconciliation
        return {
            "citations": citations,
            "retrieval": (
                "canonical-vector-dense+postgresql-fts+index-state+rrf"
                if canonical
                else "local-vector-degraded-cache+postgresql-keyword+rrf"
            ),
            "canonical": canonical,
            "authority": (
                "canonical-vector-postgresql"
                if canonical
                else "non-canonical-reconciliation-required"
            ),
            "reranker": reranker,
            "network_used": False,
        }
