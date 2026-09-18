"""Context-Augmented Generation (CAG) — Pre-loaded Context Architecture.

CAG loads relevant context upfront into the model's context window,
enabling instant retrieval without additional round-trips.

Architecture:
- Context Loader: Pre-loads relevant documents/cache into context
- Context Manager: Manages context window budget and eviction
- Context Router: Routes queries to pre-loaded context or falls back to RAG
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ..rag.orchestration.evidence import RagEvidence, RagArchitecture

_logger = logging.getLogger("gptbridge.cag")


def _evidence_score(doc: Any) -> float:
    """Best available relevance score across the evidence channels."""
    return max(
        float(getattr(doc, "dense_score", 0.0) or 0.0),
        float(getattr(doc, "sparse_score", 0.0) or 0.0),
        float(getattr(doc, "reranker_score", 0.0) or 0.0),
        float(getattr(doc, "memory_score", 0.0) or 0.0),
        float(getattr(doc, "score", 0.0) or 0.0),
    )


def _evidence_tokens(doc: Any) -> int:
    """Token estimate — RagEvidence has no token_count field."""
    explicit = int(getattr(doc, "token_count", 0) or 0)
    if explicit:
        return explicit
    return max(1, len(str(getattr(doc, "content", "") or "")) // 4)


@dataclass(frozen=True, slots=True)
class CAGContext:
    """Pre-loaded context bundle."""
    context_id: str
    module_ids: tuple[str, ...]
    documents: tuple[RagEvidence, ...]
    total_tokens: int
    created_at: float
    ttl_seconds: float
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds


@dataclass(frozen=True, slots=True)
class CAGConfig:
    """CAG configuration."""
    max_context_tokens: int = 32000
    default_ttl_seconds: float = 3600.0
    min_relevance_score: float = 0.3
    enable_preload: bool = True
    fallback_to_rag: bool = True


class ContextLoader:
    """Loads and prepares context from various sources."""

    def __init__(
        self,
        retrievers: dict[str, Callable],
        config: CAGConfig | None = None,
    ) -> None:
        self._retrievers = retrievers
        self._config = config or CAGConfig()

    def load_context(
        self,
        module_ids: tuple[str, ...],
        query_hints: tuple[str, ...] = (),
        explicit_docs: tuple[RagEvidence, ...] = (),
    ) -> CAGContext:
        """Load context for given modules."""
        documents = list(explicit_docs)

        if self._config.enable_preload and query_hints:
            for hint in query_hints:
                for arch in (RagArchitecture.HYBRID, RagArchitecture.CODE, RagArchitecture.MEMORY):
                    tool_key = self._arch_to_tool(arch)
                    fn = self._retrievers.get(tool_key)
                    if fn:
                        try:
                            results = fn(hint, {"module_ids": module_ids})
                            documents.extend(results)
                        except Exception:
                            pass

        # Deduplicate by content hash
        seen = set()
        unique_docs = []
        for doc in documents:
            content_hash = hashlib.sha256(doc.content.encode()).hexdigest()[:16]
            if content_hash not in seen:
                seen.add(content_hash)
                unique_docs.append(doc)

        # Filter by relevance — RagEvidence carries per-channel scores
        # (dense/sparse/reranker), not a single ``.score``.
        filtered = [
            d for d in unique_docs
            if _evidence_score(d) >= self._config.min_relevance_score
        ]

        total_tokens = sum(_evidence_tokens(d) for d in filtered)

        # Truncate if over budget
        if total_tokens > self._config.max_context_tokens:
            filtered.sort(key=_evidence_score, reverse=True)
            running = 0
            kept = []
            for d in filtered:
                tokens = _evidence_tokens(d)
                if running + tokens <= self._config.max_context_tokens:
                    kept.append(d)
                    running += tokens
            filtered = kept
            total_tokens = running

        context_id = hashlib.sha256(
            f"{module_ids}{query_hints}{time.time()}".encode()
        ).hexdigest()[:16]

        return CAGContext(
            context_id=context_id,
            module_ids=module_ids,
            documents=tuple(filtered),
            total_tokens=total_tokens,
            created_at=time.time(),
            ttl_seconds=self._config.default_ttl_seconds,
        )

    @staticmethod
    def _arch_to_tool(arch: RagArchitecture) -> str:
        return {
            RagArchitecture.HYBRID: "retrieve_hybrid",
            RagArchitecture.CODE: "retrieve_code",
            RagArchitecture.MEMORY: "retrieve_memory",
        }.get(arch, "retrieve_hybrid")


class ContextManager:
    """Manages active CAG contexts with LRU eviction."""

    def __init__(self, config: CAGConfig | None = None) -> None:
        self._config = config or CAGConfig()
        self._contexts: dict[str, CAGContext] = {}
        self._access_order: list[str] = []

    def store(self, context: CAGContext) -> None:
        """Store a context."""
        if context.context_id in self._contexts:
            self._access_order.remove(context.context_id)
        self._contexts[context.context_id] = context
        self._access_order.append(context.context_id)
        self._evict_if_needed()

    def get(self, context_id: str) -> Optional[CAGContext]:
        """Retrieve a context by ID."""
        ctx = self._contexts.get(context_id)
        if ctx and not ctx.is_expired():
            # Move to end (MRU)
            self._access_order.remove(context_id)
            self._access_order.append(context_id)
            return ctx
        elif ctx:
            self._remove(context_id)
        return None

    def find_by_modules(self, module_ids: tuple[str, ...]) -> Optional[CAGContext]:
        """Find a usable context for modules — an empty context must not
        hijack queries that real retrieval could answer."""
        for ctx in self._contexts.values():
            if (
                not ctx.is_expired()
                and ctx.documents
                and set(ctx.module_ids) == set(module_ids)
            ):
                return ctx
        return None

    def _evict_if_needed(self) -> None:
        """Evict expired or LRU contexts if over budget."""
        now = time.time()
        expired = [cid for cid, ctx in self._contexts.items() if now - ctx.created_at > ctx.ttl_seconds]
        for cid in expired:
            self._remove(cid)

    def _remove(self, context_id: str) -> None:
        self._contexts.pop(context_id, None)
        if context_id in self._access_order:
            self._access_order.remove(context_id)

    def stats(self) -> dict[str, Any]:
        return {
            "active_contexts": len(self._contexts),
            "total_tokens": sum(c.total_tokens for c in self._contexts.values()),
            "expired": sum(1 for c in self._contexts.values() if c.is_expired()),
        }


class ContextRouter:
    """Routes queries to CAG context or falls back to RAG."""

    def __init__(
        self,
        loader: ContextLoader,
        manager: ContextManager,
        rag_orchestrator: Any,
        config: CAGConfig | None = None,
    ) -> None:
        self._loader = loader
        self._manager = manager
        self._rag = rag_orchestrator
        self._config = config or CAGConfig()

    def query(
        self,
        query: str,
        module_ids: tuple[str, ...] = (),
        generation_mode: str = "general",
        session_id: str = "",
        explicit_architectures: tuple[Any, ...] | None = None,
        required_aspects: tuple[str, ...] = (),
        task_instruction: str = "",
        plan_id: str = "",
    ) -> Any:
        """Execute query using CAG or fallback to RAG."""
        # Try to find existing context
        context = self._manager.find_by_modules(module_ids)

        if context is None and self._config.enable_preload:
            # Load new context
            context = self._loader.load_context(module_ids)
            self._manager.store(context)

        if context and context.documents and not context.is_expired():
            # Use pre-loaded context
            _logger.info("CAG: using pre-loaded context %s (%d docs, %d tokens)",
                         context.context_id, len(context.documents), context.total_tokens)
            return self._answer_from_context(query, context, generation_mode)

        if self._config.fallback_to_rag:
            _logger.info("CAG: falling back to RAG")
            return self._rag.query(
                query,
                generation_mode=generation_mode,
                module_ids=module_ids,
                session_id=session_id,
                explicit_architectures=explicit_architectures,
                required_aspects=required_aspects,
                task_instruction=task_instruction,
                plan_id=plan_id,
            )

        raise RuntimeError("CAG: no context available and RAG fallback disabled")

    def _answer_from_context(
        self,
        query: str,
        context: CAGContext,
        generation_mode: str,
    ) -> Any:
        """Generate answer from pre-loaded context."""
        from ..rag.orchestration.context_builder import build_context, BuiltContext
        from ..rag.orchestration.generation_router import route_generation

        built = build_context(
            list(context.documents),
            system_governance="",
            task_instruction=query,
        )
        generation = route_generation(generation_mode)

        from ..rag.orchestration.orchestrator import OrchestratorResult
        from ..rag.orchestration.sufficiency import (
            SufficiencyReport, SufficiencyVerdict, evaluate_sufficiency
        )
        from ..rag.orchestration.fusion import architecture_fusion, mark_conflicts

        fused = mark_conflicts(architecture_fusion({
            RagArchitecture.HYBRID: list(context.documents)
        }))
        report = evaluate_sufficiency(fused)

        return OrchestratorResult(
            plan=None,
            evidence=context.documents,
            report=report,
            context=built,
            generation=generation,
            agentic_rounds=0,
            degraded=False,
        )


def create_cag_pipeline(
    retrievers: dict[str, Callable],
    rag_orchestrator: Any,
    config: CAGConfig | None = None,
) -> tuple[ContextLoader, ContextManager, ContextRouter]:
    """Create complete CAG pipeline."""
    cfg = config or CAGConfig()
    loader = ContextLoader(retrievers, cfg)
    manager = ContextManager(cfg)
    router = ContextRouter(loader, manager, rag_orchestrator, cfg)
    return loader, manager, router


__all__ = [
    "CAGContext",
    "CAGConfig",
    "ContextLoader",
    "ContextManager",
    "ContextRouter",
    "create_cag_pipeline",
]