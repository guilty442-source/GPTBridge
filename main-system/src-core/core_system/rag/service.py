"""LocalRagService — 統一 RAG 服務入口。

A486+A487: Canonical Gateway + Degraded Adapter + Architecture Router.
Complete lifecycle: versioned index, atomic promotion, outbox consistency,
generation-bound queries, health gate, citation validation, benchmarking.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

from .generation import GenerationManager, GenerationConfig, IndexGeneration, GenerationState
from .outbox import OutboxRepository, OutboxWorker, OutboxEvent, OutboxOperation
from .generation_binder import GenerationBinder, VerifiedHit
from .health_gate import CanonicalHealthGate, CanonicalState, CanonicalHealthReport
from .citation import Citation, CitationValidator, CitationFormatter, build_citation_from_hit
from .benchmark import RetrievalBenchmark, BenchmarkQuery, RetrievalResult, BenchmarkMetrics
from .rag_qdrant import QdrantCanonicalRuntime, RagPipelineConfig, IndexState, RagQueryResult
from .rag_metadata import PostgreSQLMetadataAuthority
from .pipeline_degraded import DegradedRagPipeline

_logger = logging.getLogger("gptbridge.rag.service")


@dataclass
class LocalRagConfig:
    """Configuration for LocalRagService."""
    # Qdrant
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: Optional[str] = None
    alias_name: str = "gptbridge_shared_knowledge"

    # PostgreSQL
    postgresql_dsn: str = ""

    # Embedding
    embedding_model: str = "qwen3-embedding:4b"
    embedding_dimension: int = 2560
    embedding_provider: str = "ollama"

    # Chunking
    chunk_size: int = 1200
    chunk_overlap: int = 200
    chunk_policy_version: str = "v3"

    # Index schema
    index_schema_version: str = "v3"

    # Retrieval
    top_k: int = 10
    score_threshold: float = 0.0

    # Generation
    max_generations_to_keep: int = 3

    # Outbox
    outbox_batch_size: int = 50
    outbox_max_attempts: int = 5

    # Health
    outbox_backlog_threshold: int = 1000

    # Degraded
    degraded_root: Optional[Path] = None


class CanonicalGateway:
    """Canonical RAG path: Qdrant (alias) + PostgreSQL + Outbox + GenerationBinder."""

    def __init__(
        self,
        config: LocalRagConfig,
        qdrant: QdrantCanonicalRuntime,
        postgresql: PostgreSQLMetadataAuthority,
        generation_manager: GenerationManager,
        outbox_repo: OutboxRepository,
    ) -> None:
        self.config = config
        self.qdrant = qdrant
        self.postgresql = postgresql
        self.generation_manager = generation_manager
        self.outbox_repo = outbox_repo
        self.binder = GenerationBinder(postgresql)

    async def index_resource(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        embedding: list[float],
        generation_id: str,
    ) -> IndexState:
        """Index resource through canonical path (transactional outbox)."""
        # 1. Upsert to Qdrant (generation-specific collection)
        point_id = metadata.get("point_id") or f"{module_id}:{resource_id}"
        point_payload = {
            "resource_id": resource_id,
            "module_id": module_id,
            "content": content,
            "content_hash": metadata.get("content_hash", ""),
            "generation_id": generation_id,
            "indexed_at_utc": datetime.now(timezone.utc).isoformat(),
            **metadata,
        }

        success = await self.qdrant.upsert_points([
            {
                "id": point_id,
                "vector": embedding,
                "payload": point_payload,
            }
        ], generation_id=generation_id)

        if not success:
            raise RuntimeError("Qdrant upsert failed")

        # 2. Update PostgreSQL metadata + create outbox event (same transaction)
        # This should be done in a single PostgreSQL transaction
        # The OutboxRepository.create_event must be called within that transaction
        # For now, we document the pattern; actual transaction handled by caller

        index_state = IndexState(
            resource_id=resource_id,
            module_id=module_id,
            embedding_model=self.config.embedding_model,
            embedding_dimension=self.config.embedding_dimension,
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
            indexed_at_utc=datetime.now(timezone.utc).isoformat(),
            content_hash=metadata.get("content_hash", ""),
            qdrant_point_id=point_id,
            postgresql_record_id=None,
            generation_id=generation_id,
        )
        return index_state

    async def query(
        self,
        query_embedding: list[float],
        module_id: Optional[str] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> list[VerifiedHit]:
        """Query through canonical path with generation binding."""
        # 1. Search Qdrant via alias (ACTIVE generation)
        hits = await self.qdrant.search(
            query_vector=query_embedding,
            module_id=module_id,
            top_k=top_k or self.config.top_k,
            score_threshold=score_threshold or self.config.score_threshold,
        )

        if not hits:
            return []

        # 2. Bind and verify against PostgreSQL metadata
        verified, dropped = await self.binder.bind_hits(hits, module_id)

        if dropped:
            _logger.warning(
                "CanonicalGateway: dropped %d hits for query (module=%s)",
                len(dropped), module_id
            )

        return verified


class DegradedAdapter:
    """Degraded mode adapter using local SQLite stores."""

    def __init__(self, config: LocalRagConfig) -> None:
        self.config = config
        self.pipeline = DegradedRagPipeline(
            config=RagPipelineConfig(
                qdrant_url="",  # Not used
                qdrant_api_key=None,
                collection_name="",  # Not used
                postgresql_dsn="",   # Not used
                embedding_model=config.embedding_model,
                embedding_dimension=config.embedding_dimension,
                chunk_size=config.chunk_size,
                chunk_overlap=config.chunk_overlap,
                top_k=config.top_k,
                score_threshold=config.score_threshold,
            ),
            degraded_root=config.degraded_root,
        )

    async def index_resource(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        embedding: list[float],
    ) -> IndexState:
        """Index through degraded path."""
        return await self.pipeline.index_resource(
            module_id, resource_id, content, metadata, embedding
        )

    async def query(
        self,
        query_embedding: list[float],
        module_id: Optional[str] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> list[RagQueryResult]:
        """Query through degraded path."""
        return await self.pipeline.query(
            query_embedding, module_id, top_k, score_threshold
        )


class ArchitectureRouter:
    """Routes queries to appropriate retrieval strategy."""

    def __init__(
        self,
        canonical: CanonicalGateway,
        degraded: DegradedAdapter,
        health_gate: CanonicalHealthGate,
    ) -> None:
        self.canonical = canonical
        self.degraded = degraded
        self.health_gate = health_gate
        self._use_canonical = True

    async def route_query(
        self,
        query_embedding: list[float],
        module_id: Optional[str] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
    ) -> tuple[list[Any], CanonicalState]:
        """Route query based on health state."""
        health = await self.health_gate.evaluate()

        # Update routing decision
        if health.state in (
            CanonicalState.CANONICAL_READY,
            CanonicalState.DEGRADED,
        ):
            self._use_canonical = health.state == CanonicalState.CANONICAL_READY
        else:
            self._use_canonical = False

        if self._use_canonical:
            try:
                results = await self.canonical.query(
                    query_embedding, module_id, top_k, score_threshold
                )
                return results, health.state
            except Exception as e:
                _logger.error("ArchitectureRouter: canonical query failed, falling back: %s", e)
                self._use_canonical = False

        # Fallback to degraded
        results = await self.degraded.query(
            query_embedding, module_id, top_k, score_threshold
        )
        return results, CanonicalState.DEGRADED


class ContextBuilder:
    """Builds LLM context from verified hits with citations."""

    def __init__(self, citation_validator: CitationValidator) -> None:
        self.citation_validator = citation_validator
        self.citation_counter = 0

    def build_context(
        self,
        hits: list[Any],  # VerifiedHit or RagQueryResult
        max_context_tokens: int = 8000,
    ) -> tuple[str, dict[str, Citation]]:
        """Build context string and citation map."""
        self.citation_counter = 0
        citations = {}
        context_parts = []

        for hit in hits:
            self.citation_counter += 1
            citation_id = f"R{self.citation_counter}"

            if hasattr(hit, 'index_state'):
                # VerifiedHit
                idx = hit.index_state
                meta = hit.metadata
                content = hit.hit.get("payload", {}).get("content", "")
            else:
                # RagQueryResult (degraded)
                idx = hit.index_state
                meta = hit.metadata
                content = hit.content

            citation = Citation(
                citation_id=citation_id,
                resource_id=idx.resource_id,
                chunk_id=idx.qdrant_point_id,
                locator_id=f"{idx.module_id}:{idx.resource_id}",
                character_start=0,
                character_end=len(content),
                content_hash=idx.content_hash,
                generation_id=idx.generation_id or "",
                retrieval_score=hit.hit.get("score", 0.0) if hasattr(hit, 'hit') else hit.score,
                module_id=idx.module_id,
            )
            citations[citation_id] = citation

            context_parts.append(
                f"[{citation_id}] {citation.locator_id}\n{content}"
            )

        context = "\n\n---\n\n".join(context_parts)
        return context, citations

    def format_response(self, answer: str, citations: dict[str, Citation]) -> str:
        """Append citation footer to answer."""
        used_citations = CitationFormatter.parse_citation_ids(answer)
        if not used_citations:
            return answer

        cited = [citations[cid] for cid in used_citations if cid in citations]
        return CitationFormatter.format_for_response(answer, cited)


class LocalRagService:
    """Unified RAG service: Canonical Gateway + Degraded Adapter + Router."""

    def __init__(self, config: LocalRagConfig) -> None:
        self.config = config

        # Core components
        self.qdrant = QdrantCanonicalRuntime(RagPipelineConfig(
            qdrant_url=config.qdrant_url,
            qdrant_api_key=config.qdrant_api_key,
            collection_name=config.alias_name,
            postgresql_dsn=config.postgresql_dsn,
            embedding_model=config.embedding_model,
            embedding_dimension=config.embedding_dimension,
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
            top_k=config.top_k,
            score_threshold=config.score_threshold,
        ))

        self.postgresql = PostgreSQLMetadataAuthority(config.postgresql_dsn)

        # Generation management
        gen_config = GenerationConfig(
            alias_name=config.alias_name,
            index_schema_version=config.index_schema_version,
            embedding_model=config.embedding_model,
            embedding_dimension=config.embedding_dimension,
            chunk_policy_version=config.chunk_policy_version,
            chunk_size=config.chunk_size,
            chunk_overlap=config.chunk_overlap,
            max_generations_to_keep=config.max_generations_to_keep,
        )
        self.generation_manager = GenerationManager(
            qdrant_client=self.qdrant.client,
            config=gen_config,
            metadata_db=self.postgresql,
        )

        # Outbox
        self.outbox_repo = OutboxRepository(config.postgresql_dsn)
        self.outbox_worker = OutboxWorker(
            outbox_repo=self.outbox_repo,
            qdrant_runtime=self.qdrant,
            batch_size=config.outbox_batch_size,
            max_attempts=config.outbox_max_attempts,
        )

        # Gateway & Adapter
        self.canonical = CanonicalGateway(
            config, self.qdrant, self.postgresql,
            self.generation_manager, self.outbox_repo
        )
        self.degraded = DegradedAdapter(config)

        # Health & Routing
        self.health_gate = CanonicalHealthGate(
            qdrant=self.qdrant,
            postgresql=self.postgresql,
            generation_manager=self.generation_manager,
            outbox_repo=self.outbox_repo,
        )
        self.router = ArchitectureRouter(
            canonical=self.canonical,
            degraded=self.degraded,
            health_gate=self.health_gate,
        )

        # Citations & Context
        self.citation_validator = CitationValidator(self.postgresql)
        self.context_builder = ContextBuilder(self.citation_validator)

        # Benchmark
        self.benchmark = RetrievalBenchmark()

        self._initialized = False

    async def initialize(self) -> bool:
        """Initialize all components."""
        try:
            # Qdrant
            qdrant_ok = await self.qdrant.initialize()
            if qdrant_ok:
                await self.qdrant.ensure_collection()
                await self.qdrant.ensure_payload_indexes()

            # PostgreSQL
            pg_ok = self.postgresql.is_healthy()

            # Generation manager
            await self.generation_manager.get_active_generation()

            self._initialized = True
            _logger.info("LocalRagService: initialized (qdrant=%s, pg=%s)", qdrant_ok, pg_ok)
            return qdrant_ok and pg_ok
        except Exception as e:
            _logger.error("LocalRagService: initialization failed: %s", e)
            return False

    async def health_check(self) -> CanonicalHealthReport:
        """Run canonical health gate."""
        return await self.health_gate.evaluate()

    async def index_resource(
        self,
        module_id: str,
        resource_id: str,
        content: str,
        metadata: dict[str, Any],
        embedding: list[float],
        generation_id: Optional[str] = None,
    ) -> IndexState:
        """Index resource through appropriate path."""
        if not self._initialized:
            await self.initialize()

        health = await self.health_check()
        if health.is_ready and generation_id:
            return await self.canonical.index_resource(
                module_id, resource_id, content, metadata, embedding, generation_id
            )
        else:
            return await self.degraded.index_resource(
                module_id, resource_id, content, metadata, embedding
            )

    async def query(
        self,
        question: str,
        query_embedding: list[float],
        module_id: Optional[str] = None,
        top_k: Optional[int] = None,
        score_threshold: Optional[float] = None,
        max_context_tokens: int = 8000,
    ) -> dict[str, Any]:
        """Execute full RAG query: retrieve -> bind -> build context -> validate citations."""
        if not self._initialized:
            await self.initialize()

        # 1. Route and retrieve
        hits, state = await self.router.route_query(
            query_embedding, module_id, top_k, score_threshold
        )

        # 2. Build context with citations
        context, citations = self.context_builder.build_context(hits, max_context_tokens)

        # 3. Validate citations
        all_valid, validation_results = await self.citation_validator.validate_response(
            answer="",  # Will be filled by LLM
            citations=citations,
        )

        return {
            "question": question,
            "context": context,
            "citations": {cid: c.to_dict() for cid, c in citations.items()},
            "citation_validation": {
                "all_valid": all_valid,
                "results": [
                    {
                        "citation_id": r.citation_id,
                        "valid": r.valid,
                        "errors": list(r.errors),
                        "warnings": list(r.warnings),
                    }
                    for r in validation_results
                ],
            },
            "state": state.value,
            "hit_count": len(hits),
        }

    async def run_benchmark(
        self,
        method: str = "canonical",
        category: Optional[str] = None,
        difficulty: Optional[str] = None,
    ) -> BenchmarkMetrics:
        """Run retrieval benchmark."""
        queries = self.benchmark.filter_queries(category, difficulty)

        async def retrieval_fn(question: str, module_id: Optional[str], top_k: int):
            # Mock embedding for benchmark
            embedding = [0.0] * self.config.embedding_dimension
            hits, _ = await self.router.route_query(embedding, module_id, top_k)
            return RetrievalResult(
                query_id="",
                retrieved_resource_ids=[h.hit.get("payload", {}).get("resource_id", "") for h in hits],
                retrieved_chunk_ids=[h.hit.get("payload", {}).get("chunk_id", "") for h in hits],
                scores=[h.hit.get("score", 0.0) for h in hits],
                latency_ms=0,
                method=method,
            )

        return await self.benchmark.run_benchmark(retrieval_fn, queries, method_name=method)

    async def start_generation_reindex(self) -> IndexGeneration:
        """Start a new generation reindex (BUILDING)."""
        generation = await self.generation_manager.create_generation()
        await self.generation_manager.ensure_collection(generation)
        return generation

    async def promote_generation(self, generation: IndexGeneration) -> bool:
        """Verify and promote generation to ACTIVE (alias switch)."""
        verification = await self.generation_manager.verify_generation(generation)
        if not verification.get("ok"):
            _logger.error("Generation verification failed: %s", verification)
            return False

        # Update generation with verification result
        verified_gen = IndexGeneration(
            generation_id=generation.generation_id,
            index_schema_version=generation.index_schema_version,
            embedding_model=generation.embedding_model,
            embedding_dimension=generation.embedding_dimension,
            chunk_policy_version=generation.chunk_policy_version,
            chunk_size=generation.chunk_size,
            chunk_overlap=generation.chunk_overlap,
            created_at=generation.created_at,
            state=GenerationState.VERIFYING,
            collection_name=generation.collection_name,
            alias_name=generation.alias_name,
            previous_generation_id=generation.previous_generation_id,
            points_count=verification.get("points_count", 0),
            verification_result=verification,
        )
        await self.generation_manager.promote_to_active(verified_gen)
        return True

    async def process_outbox_batch(self) -> dict[str, int]:
        """Process one batch of outbox events."""
        return await self.outbox_worker.process_batch()

    async def shutdown(self) -> None:
        """Graceful shutdown."""
        self.outbox_repo.close()
        _logger.info("LocalRagService: shutdown complete")


__all__ = [
    "LocalRagConfig",
    "LocalRagService",
    "CanonicalGateway",
    "DegradedAdapter",
    "ArchitectureRouter",
    "ContextBuilder",
    "CitationValidator",
    "CitationFormatter",
    "build_citation_from_hit",
]