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

            # Wrong-module hit rate
            if query.module_id:
                for rid in retrieved:
                    # In production: check if rid belongs to different module
                    pass

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
            p95_latency_ms=self._percentile(latencies, 95) if latencies else 0,
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

    @staticmethod
    def _percentile(values: list[float], p: float) -> float:
        """Compute percentile."""
        if not values:
            return 0.0
        sorted_vals = sorted(values)
        idx = int(len(sorted_vals) * p / 100)
        return sorted_vals[min(idx, len(sorted_vals) - 1)]

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
    "create_sample_test_set",
]