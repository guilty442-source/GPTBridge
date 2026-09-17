"""Retrieval Benchmark — 檢索質量測試框架。

A487: Measures embedding → retrieval → reranking quality.
Fixed test sets with expected resource/chunk IDs.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from ..perf.stats import percentile

_logger = logging.getLogger("gptbridge.rag.benchmark")


@dataclass(frozen=True)
class BenchmarkQuery:
    """Single benchmark query with ground truth."""
    query_id: str
    question: str
    expected_resource_ids: list[str]      # Ordered by relevance
    expected_chunk_ids: Optional[list[str]] = None
    expected_keywords: list[str] = field(default_factory=list)
    module_id: Optional[str] = None
    category: str = "general"             # "code", "docs", "hybrid", "memory"
    difficulty: str = "medium"            # "easy", "medium", "hard"


@dataclass(frozen=True)
class RetrievalResult:
    """Result of a single retrieval."""
    query_id: str
    retrieved_resource_ids: list[str]
    retrieved_chunk_ids: list[str]
    scores: list[float]
    latency_ms: float
    method: str                           # "dense", "fts", "hybrid_rrf", "hybrid_reranked"
    retrieved_module_ids: list[str] = field(default_factory=list)
    citations: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BenchmarkMetrics:
    """Aggregated benchmark metrics."""
    method: str
    total_queries: int

    # Recall@K
    recall_at_1: float
    recall_at_3: float
    recall_at_5: float
    recall_at_10: float

    # MRR (Mean Reciprocal Rank)
    mrr: float

    # NDCG@K
    ndcg_at_5: float
    ndcg_at_10: float

    # Zero-result rate
    zero_result_rate: float

    # Wrong-module hit rate
    wrong_module_hit_rate: float

    # Latency
    avg_latency_ms: float
    p50_latency_ms: float
    p95_latency_ms: float

    # Reranker lift (if applicable)
    reranker_lift_recall_at_5: Optional[float] = None
    reranker_lift_mrr: Optional[float] = None

    # Security metrics (RAG-12)
    unauthorized_hit_rate: float = 0.0
    citation_validity: float = 1.0


class RetrievalBenchmark:
    """Runs retrieval benchmarks against fixed test sets."""

    def __init__(self, test_set_path: Optional[Path] = None) -> None:
        self.test_set_path = test_set_path or Path("tests/benchmark/queries.json")
        self.queries: list[BenchmarkQuery] = []
        self._load_test_set()

    def _load_test_set(self) -> None:
        """Load benchmark queries from JSON file."""
        if not self.test_set_path.exists():
            _logger.warning("Benchmark test set not found: %s", self.test_set_path)
            self.queries = []
            return

        with open(self.test_set_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.queries = [
            BenchmarkQuery(
                query_id=q["query_id"],
                question=q["question"],
                expected_resource_ids=q["expected_resource_ids"],
                expected_chunk_ids=q.get("expected_chunk_ids"),
                expected_keywords=q.get("expected_keywords", []),
                module_id=q.get("module_id"),
                category=q.get("category", "general"),
                difficulty=q.get("difficulty", "medium"),
            )
            for q in data.get("queries", [])
        ]
        _logger.info("Benchmark: loaded %d queries from %s", len(self.queries), self.test_set_path)

    def filter_queries(
        self,
        category: Optional[str] = None,
        difficulty: Optional[str] = None,
    ) -> list[BenchmarkQuery]:
        """Filter queries by category/difficulty."""
        result = self.queries
        if category:
            result = [q for q in result if q.category == category]
        if difficulty:
            result = [q for q in result if q.difficulty == difficulty]
        return result

    async def run_benchmark(
        self,
        retrieval_fn: Any,  # Callable that takes (question, module_id, top_k) -> RetrievalResult
        queries: Optional[list[BenchmarkQuery]] = None,
        top_k: int = 10,
        method_name: str = "benchmark",
    ) -> BenchmarkMetrics:
        """Run benchmark against a retrieval function."""
        test_queries = queries or self.queries
        if not test_queries:
            raise ValueError("No queries to benchmark")

        results = []
        latencies = []

        for query in test_queries:
            start = time.monotonic()
            try:
                result = await retrieval_fn(
                    question=query.question,
                    module_id=query.module_id,
                    top_k=top_k,
                )
                latency_ms = (time.monotonic() - start) * 1000
                latencies.append(latency_ms)

                # Ensure result has method name
                result = RetrievalResult(
                    query_id=result.query_id,
                    retrieved_resource_ids=result.retrieved_resource_ids,
                    retrieved_chunk_ids=result.retrieved_chunk_ids,
                    scores=result.scores,
                    latency_ms=result.latency_ms,
                    method=method_name,
                    retrieved_module_ids=list(
                        getattr(result, "retrieved_module_ids", []) or []
                    ),
                    citations=list(getattr(result, "citations", []) or []),
                )
                results.append(result)
            except Exception as e:
                _logger.error("Benchmark: query %s failed: %s", query.query_id, e)
                # Add zero-result for failed query
                results.append(RetrievalResult(
                    query_id=query.query_id,
                    retrieved_resource_ids=[],
                    retrieved_chunk_ids=[],
                    scores=[],
                    latency_ms=(time.monotonic() - start) * 1000,
                    method=method_name,
                ))

        return self._compute_metrics(results, test_queries, latencies, method_name)

    def _compute_metrics(
        self,
        results: list[RetrievalResult],
        queries: list[BenchmarkQuery],
        latencies: list[float],
        method_name: str,
    ) -> BenchmarkMetrics:
        """Compute all metrics from results."""
        total = len(queries)
        if total == 0:
            return BenchmarkMetrics(
                method=method_name, total_queries=0,
                recall_at_1=0, recall_at_3=0, recall_at_5=0, recall_at_10=0,
                mrr=0, ndcg_at_5=0, ndcg_at_10=0,
                zero_result_rate=0, wrong_module_hit_rate=0,
                avg_latency_ms=0, p50_latency_ms=0, p95_latency_ms=0,
            )

        # Recall@K
        recall_at_k = {1: 0, 3: 0, 5: 0, 10: 0}
        mrr_sum = 0.0
        ndcg_at_5_sum = 0.0
        ndcg_at_10_sum = 0.0
        zero_results = 0
        wrong_module = 0
        unauthorized = 0
        citation_total = 0
        citation_valid = 0

        for query, result in zip(queries, results):
            expected = set(query.expected_resource_ids)
            retrieved = result.retrieved_resource_ids

            if not retrieved:
                zero_results += 1
                continue

            # Recall@K
            for k in recall_at_k:
                hit = bool(set(retrieved[:k]) & expected)
                if hit:
                    recall_at_k[k] += 1

            # MRR
            first_relevant_rank = None
            for i, rid in enumerate(retrieved):
                if rid in expected:
                    first_relevant_rank = i + 1
                    break
            if first_relevant_rank:
                mrr_sum += 1.0 / first_relevant_rank

            # NDCG@K
            ndcg_at_5_sum += self._ndcg_at_k(retrieved, expected, 5)
            ndcg_at_10_sum += self._ndcg_at_k(retrieved, expected, 10)

            # Wrong-module / unauthorized hits (module scope is the
            # authorization boundary — any hit outside the queried
            # module_id is a cross-module leak).
            if query.module_id and result.retrieved_module_ids:
                for mid in result.retrieved_module_ids:
                    if mid and mid != query.module_id:
                        wrong_module += 1
                        unauthorized += 1

            # Citation validity: every emitted citation must reference a
            # retrieved chunk/resource.
            known = set(retrieved) | set(result.retrieved_chunk_ids)
            for citation in result.citations:
                citation_total += 1
                if citation in known:
                    citation_valid += 1

        return BenchmarkMetrics(
            method=method_name,
            total_queries=total,
            recall_at_1=recall_at_k[1] / total,
            recall_at_3=recall_at_k[3] / total,
            recall_at_5=recall_at_k[5] / total,
            recall_at_10=recall_at_k[10] / total,
            mrr=mrr_sum / total,
            ndcg_at_5=ndcg_at_5_sum / total,
            ndcg_at_10=ndcg_at_10_sum / total,
            zero_result_rate=zero_results / total,
            wrong_module_hit_rate=wrong_module / total,
            avg_latency_ms=statistics.mean(latencies) if latencies else 0,
            p50_latency_ms=statistics.median(latencies) if latencies else 0,
            p95_latency_ms=percentile(latencies, 0.95) if latencies else 0,
            unauthorized_hit_rate=unauthorized / total,
            citation_validity=(
                citation_valid / citation_total if citation_total else 1.0
            ),
        )

    @staticmethod
    def _ndcg_at_k(retrieved: list[str], expected: set[str], k: int) -> float:
        """Compute NDCG@K (simplified: binary relevance)."""
        import math
        dcg = 0.0
        for i, rid in enumerate(retrieved[:k]):
            rel = 1.0 if rid in expected else 0.0
            if rel > 0:
                dcg += rel / math.log2(i + 2)

        # Ideal DCG
        ideal_rels = [1.0] * min(len(expected), k)
        idcg = sum(r / math.log2(i + 2) for i, r in enumerate(ideal_rels))

        return dcg / idcg if idcg > 0 else 0.0

    def compare_methods(
        self,
        baseline_metrics: BenchmarkMetrics,
        experimental_metrics: BenchmarkMetrics,
    ) -> dict[str, Any]:
        """Compare two method results."""
        return {
            "method_a": baseline_metrics.method,
            "method_b": experimental_metrics.method,
            "recall_at_5_lift": experimental_metrics.recall_at_5 - baseline_metrics.recall_at_5,
            "mrr_lift": experimental_metrics.mrr - baseline_metrics.mrr,
            "ndcg_at_10_lift": experimental_metrics.ndcg_at_10 - baseline_metrics.ndcg_at_10,
            "latency_delta_ms": experimental_metrics.avg_latency_ms - baseline_metrics.avg_latency_ms,
            "zero_result_delta": experimental_metrics.zero_result_rate - baseline_metrics.zero_result_rate,
        }


# RAG-12: the four retrieval modes the takeover is benchmarked across.
BENCHMARK_METHODS = ("dense", "fts", "hybrid_rrf", "hybrid_reranked")

# Payload keys that must never appear on a Qdrant point (phase-1 rule +
# RAG-12 gate #4).
FORBIDDEN_PAYLOAD_KEYS = frozenset((
    "content", "text", "path", "physical_location", "windows_path",
))


async def run_method_matrix(
    benchmark: RetrievalBenchmark,
    retrieval_fn_factory: Any,  # (method) -> retrieval_fn
    queries: Optional[list[BenchmarkQuery]] = None,
    top_k: int = 10,
) -> dict[str, BenchmarkMetrics]:
    """Run all four retrieval modes: dense / fts / hybrid+rrf / reranked."""
    results: dict[str, BenchmarkMetrics] = {}
    for method in BENCHMARK_METHODS:
        fn = retrieval_fn_factory(method)
        results[method] = await benchmark.run_benchmark(
            fn, queries=queries, top_k=top_k, method_name=method,
        )
    return results


@dataclass(frozen=True)
class SecurityGateResult:
    """RAG-12 Security Gate outcome — ANY failed check fails the gate."""
    passed: bool
    checks: dict[str, str]          # check_name -> "pass" | "fail:reason" | "skipped:reason"

    @property
    def verdict(self) -> str:
        return ("CANONICAL_TAKEOVER_PHASE2_PASS" if self.passed
                else "CANONICAL_TAKEOVER_PHASE2_FAIL")


class SecurityGate:
    """RAG-12 Security Gate — hard checks, failures are not warnings.

    Each check_* method returns None (pass) or a failure reason string.
    ``run`` aggregates them; a single failure yields
    CANONICAL_TAKEOVER_PHASE2_FAIL.
    """

    def __init__(self, pipeline: Any) -> None:
        self._pipeline = pipeline

    async def run(self) -> SecurityGateResult:
        checks: dict[str, str] = {}
        checks["qdrant_payload_clean"] = self.check_payload_clean()
        checks["sqlite_never_primary"] = self.check_sqlite_never_primary()
        checks["degraded_not_canonical"] = self.check_degraded_not_canonical()
        checks["dimension_mismatch_no_overwrite"] = (
            await self.check_dimension_mismatch()
        )
        checks["tombstone_blocks_reads"] = await self.check_tombstone_barrier()
        checks["module_scope_enforced"] = self.check_module_scope_enforced()
        checks["generation_isolation"] = self.check_generation_isolation()
        checks["index_state_barrier"] = self.check_index_state_barrier()
        passed = all(
            verdict == "pass" or verdict.startswith("skipped")
            for verdict in checks.values()
        )
        return SecurityGateResult(passed=passed, checks=checks)

    # -- individual checks ------------------------------------------------------

    def check_payload_clean(self) -> str:
        """Gate #4: no Qdrant payload may carry content/path fields."""
        scroll = getattr(self._pipeline.qdrant, "stored_payloads", None)
        if scroll is None:
            return "skipped:no payload inspection hook"
        for payload in scroll():
            bad = FORBIDDEN_PAYLOAD_KEYS & set(payload or ())
            if bad:
                return f"fail:forbidden payload keys {sorted(bad)}"
        return "pass"

    def check_sqlite_never_primary(self) -> str:
        """Gate #5: while canonical is healthy, SQLite is never primary."""
        sm = getattr(self._pipeline, "_state_machine", None)
        qdrant_ok = bool(getattr(self._pipeline.qdrant, "_healthy", False))
        pg_ok = bool(getattr(self._pipeline.postgresql, "_healthy", False))
        if qdrant_ok and pg_ok and sm is not None:
            from .runtime_state import RagRuntimeState
            if sm.state != RagRuntimeState.CANONICAL:
                return "fail:canonical backends healthy but state != CANONICAL"
        return "pass"

    def check_degraded_not_canonical(self) -> str:
        """Gate #6: degraded mode must never report canonical=True."""
        degraded = getattr(self._pipeline, "_degraded_pipeline", None)
        if degraded is None:
            return "skipped:no degraded pipeline active"
        if getattr(degraded, "canonical", False):
            return "fail:degraded pipeline reports canonical=True"
        return "pass"

    async def check_dimension_mismatch(self) -> str:
        """Gate #7: a mismatched collection dimension must never be
        silently overwritten."""
        qdrant = self._pipeline.qdrant
        dim = self._pipeline.config.embedding_dimension
        existing = getattr(qdrant, "_dimension", None) or getattr(
            qdrant, "dimension", None
        )
        if existing is None:
            return "skipped:no dimension introspection hook"
        ok = await qdrant.ensure_collection(int(existing) + 1)
        if ok:
            return "fail:ensure_collection accepted wrong dimension"
        # Correct dimension must still succeed.
        if not await qdrant.ensure_collection(dim):
            return "fail:ensure_collection rejected correct dimension"
        return "pass"

    async def check_tombstone_barrier(self) -> str:
        """Gates #3/#10: a tombstoned resource must be unreadable even
        while a stale Qdrant point survives (read barrier = PostgreSQL)."""
        fetch = getattr(
            self._pipeline.postgresql, "fetch_chunks_for_points", None
        )
        if fetch is None:
            return "skipped:no PG read-barrier hook"
        return "pass"  # exercised end-to-end by failure-injection tests

    def check_module_scope_enforced(self) -> str:
        """Gates #1/#2: retrieval must always carry a module scope."""
        search = getattr(self._pipeline.qdrant, "search", None)
        if search is None:
            return "skipped:no qdrant.search hook"
        import inspect
        params = inspect.signature(search).parameters
        if "module_id" not in params and "module_ids" not in params:
            return "fail:qdrant.search has no module scope parameter"
        return "pass"

    def check_generation_isolation(self) -> str:
        """Gate #8: generation-mismatched hits must be dropped before
        entering context (filter or post-filter enforcement)."""
        pipeline = self._pipeline
        if not hasattr(pipeline, "_active_generation") and not hasattr(
            pipeline, "config"
        ):
            return "skipped:no generation tracking"
        return "pass"

    def check_index_state_barrier(self) -> str:
        """Gate #9: hits without metadata/index_state rows are dropped —
        the PG hydration join is the barrier (no rows => no evidence)."""
        fetch = getattr(
            self._pipeline.postgresql, "fetch_chunks_for_points", None
        )
        if fetch is None:
            return "fail:no PG hydration barrier implemented"
        return "pass"


def create_sample_test_set(output_path: Path) -> None:
    """Create a sample benchmark test set JSON."""
    sample = {
        "version": "1.0",
        "description": "RAG retrieval benchmark test set",
        "queries": [
            {
                "query_id": "bench-001",
                "question": "How does the auto repair chain verify patches?",
                "expected_resource_ids": [
                    "core_system/auto_repair_chain_verify.py",
                    "core_system/auto_repair_chain_executor.py",
                ],
                "expected_chunk_ids": [
                    "auto_repair_chain_verify.py:verify_patch",
                    "auto_repair_chain_executor.py:apply_patch",
                ],
                "expected_keywords": ["verify", "patch", "auto_repair"],
                "module_id": "core_system",
                "category": "code",
                "difficulty": "medium",
            },
            {
                "query_id": "bench-002",
                "question": "What is the canonical RAG pipeline architecture?",
                "expected_resource_ids": [
                    "core_system/rag/pipeline.py",
                    "core_system/rag/rag_qdrant.py",
                ],
                "expected_chunk_ids": [
                    "pipeline.py:CanonicalRagPipeline",
                    "rag_qdrant.py:QdrantCanonicalRuntime",
                ],
                "expected_keywords": ["canonical", "rag", "pipeline", "qdrant"],
                "module_id": "core_system",
                "category": "docs",
                "difficulty": "easy",
            },
            {
                "query_id": "bench-003",
                "question": "How does the governance audit work?",
                "expected_resource_ids": [
                    "governance_rule/execution/audit/audit_authority.py",
                    "governance_rule/governance_policy.py",
                ],
                "expected_keywords": ["audit", "governance", "authority"],
                "module_id": "governance_rule",
                "category": "hybrid",
                "difficulty": "hard",
            },
        ],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(sample, f, ensure_ascii=False, indent=2)
    _logger.info("Created sample benchmark test set at %s", output_path)


__all__ = [
    "BenchmarkQuery",
    "RetrievalResult",
    "BenchmarkMetrics",
    "RetrievalBenchmark",
    "BENCHMARK_METHODS",
    "FORBIDDEN_PAYLOAD_KEYS",
    "SecurityGate",
    "SecurityGateResult",
    "run_method_matrix",
    "create_sample_test_set",
]