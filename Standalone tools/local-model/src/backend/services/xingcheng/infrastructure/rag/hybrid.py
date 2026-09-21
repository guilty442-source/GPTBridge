"""Hybrid Retriever for Chinese Semantic Engine v2.

Implements dense + sparse + semantic fusion retrieval using Qdrant
and local vector stores.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from shared_layer.local.vector_store import LocalVectorStore as VectorStore


@dataclass(frozen=True)
class RetrievalResult:
    """Single retrieval result."""
    id: str
    score: float
    payload: dict[str, Any]
    source: str  # "dense", "sparse", "semantic", "fusion"


class HybridRetriever:
    """Hybrid retrieval combining dense, sparse, and semantic search.

    Retrieval methods:
    - dense: Vector similarity search via LocalVectorStore
    - sparse: BM25 keyword search via local SQLite
    - semantic: Semantic understanding via xingcheng native model
    - fusion: Weighted combination of all methods
    - rrf: Reciprocal Rank Fusion
    """

    def __init__(
        self,
        vector_store: VectorStore,
        *,
        dense_weight: float = 0.5,
        sparse_weight: float = 0.3,
        semantic_weight: float = 0.2,
    ) -> None:
        self._vector_store = vector_store
        self._dense_weight = dense_weight
        self._sparse_weight = sparse_weight
        self._semantic_weight = semantic_weight

    async def retrieve(
        self,
        *,
        query: str,
        method: str = "semantic-fusion",
        top_k: int = 10,
        module_ids: tuple[str, ...] = ("chinese-semantic-engine",),
    ) -> list[dict[str, Any]]:
        """Retrieve relevant documents using the specified method.

        Args:
            query: Search query text
            method: One of "dense", "sparse", "semantic", "semantic-fusion", "rrf"
            top_k: Maximum number of results
            module_ids: Module IDs for vector store scope

        Returns:
            List of retrieval results with scores and metadata
        """
        if method == "dense":
            return await self._dense_retrieve(query, top_k, module_ids)
        elif method == "sparse":
            return await self._sparse_retrieve(query, top_k)
        elif method == "semantic":
            return await self._semantic_retrieve(query, top_k, module_ids)
        elif method == "semantic-fusion":
            return await self._fusion_retrieve(query, top_k, module_ids)
        elif method == "rrf":
            return await self._rrf_retrieve(query, top_k, module_ids)
        else:
            raise ValueError(f"Unknown retrieval method: {method}")

    def _embed_text(self, text: str) -> list[float]:
        """Generate embedding vector for text using local hashing n-gram."""
        import re
        import hashlib
        import math

        _TOKENS = re.compile(r"[a-z0-9\u4e00-\u9fff]+")
        _DIMENSION = 256
        _NGRAM = 3

        tokens = _TOKENS.findall(text.lower())
        vec = [0.0] * _DIMENSION
        for token in tokens:
            for i in range(len(token) - _NGRAM + 1):
                gram = token[i : i + _NGRAM]
                h = int(hashlib.md5(gram.encode()).hexdigest(), 16)
                idx = h % _DIMENSION
                vec[idx] += 1.0
        # Normalize
        norm = math.sqrt(sum(v * v for v in vec))
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec

    async def _dense_retrieve(
        self, query: str, top_k: int, module_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Dense vector similarity search via LocalVectorStore."""
        try:
            vector = self._embed_text(query)
            results = self._vector_store.query(
                vector=vector,
                limit=top_k,
                module_ids=module_ids,
            )
            return [
                {
                    "id": str(r.get("id", "")),
                    "score": float(r.get("score", 0.0)),
                    "payload": {k: v for k, v in r.items() if k not in ("id", "score", "vector_score", "point_id", "document_id", "module_id")},
                    "source": "dense",
                }
                for r in results
            ]
        except Exception:
            return []

    async def _sparse_retrieve(self, query: str, top_k: int) -> list[dict[str, Any]]:
        """Sparse BM25 keyword search via local SQLite."""
        try:
            from services.xingcheng.infrastructure.local_sqlite_pool import LocalSqlitePool
            from pathlib import Path

            pool = LocalSqlitePool(Path.cwd())
            conn = pool.rag_dsn()
            import sqlite3
            conn = sqlite3.connect(conn)
            conn.row_factory = sqlite3.Row

            # Simple keyword-based search on document chunks
            keywords = query.split()
            if not keywords:
                return []

            placeholders = ",".join("?" * len(keywords))
            sql = f"""
                SELECT id, content, metadata,
                       ({" + ".join(["(content LIKE ?)" for _ in keywords])}) as score
                FROM rag_chunks
                WHERE {" OR ".join(["content LIKE ?" for _ in keywords])}
                ORDER BY score DESC
                LIMIT ?
            """
            params = []
            for kw in keywords:
                params.append(f"%{kw}%")
            for kw in keywords:
                params.append(f"%{kw}%")
            params.append(top_k)

            cursor = conn.execute(sql, params)
            rows = cursor.fetchall()
            conn.close()

            return [
                {
                    "id": str(row["id"]),
                    "score": float(row["score"]),
                    "payload": {"content": row["content"], "metadata": row["metadata"]},
                    "source": "sparse",
                }
                for row in rows
            ]
        except Exception:
            return []

    async def _semantic_retrieve(
        self, query: str, top_k: int, module_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Semantic retrieval via xingcheng native model understanding."""
        try:
            from services.xingcheng.infrastructure.chinese_semantic_engine import ChineseSemanticEngine

            engine = ChineseSemanticEngine()
            analysis = engine.analyze(query)

            # Use extracted entities and keywords for retrieval
            entities = analysis.entities
            keywords = list(analysis.keywords)

            # Build a semantic query from the analysis
            semantic_query = " ".join(keywords[:5]) if keywords else query

            return await self._dense_retrieve(semantic_query, top_k, module_ids)
        except Exception:
            return []

    async def _fusion_retrieve(
        self, query: str, top_k: int, module_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Weighted fusion of dense, sparse, and semantic retrieval."""
        # Run all three methods in parallel
        dense_task = asyncio.create_task(self._dense_retrieve(query, top_k * 2, module_ids))
        sparse_task = asyncio.create_task(self._sparse_retrieve(query, top_k * 2))
        semantic_task = asyncio.create_task(self._semantic_retrieve(query, top_k * 2, module_ids))

        dense_results, sparse_results, semantic_results = await asyncio.gather(
            dense_task, sparse_task, semantic_task, return_exceptions=True
        )

        # Handle exceptions
        if isinstance(dense_results, Exception):
            dense_results = []
        if isinstance(sparse_results, Exception):
            sparse_results = []
        if isinstance(semantic_results, Exception):
            semantic_results = []

        # Combine and score
        combined = {}

        for r in dense_results:
            key = r["id"]
            if key not in combined:
                combined[key] = {"result": r, "score": 0.0}
            combined[key]["score"] += r["score"] * self._dense_weight

        for r in sparse_results:
            key = r["id"]
            if key not in combined:
                combined[key] = {"result": r, "score": 0.0}
            combined[key]["score"] += r["score"] * self._sparse_weight

        for r in semantic_results:
            key = r["id"]
            if key not in combined:
                combined[key] = {"result": r, "score": 0.0}
            combined[key]["score"] += r["score"] * self._semantic_weight

        # Sort by combined score and return top_k
        sorted_results = sorted(
            combined.values(),
            key=lambda x: x["score"],
            reverse=True,
        )

        return [
            {
                **item["result"],
                "score": item["score"],
                "source": "fusion",
            }
            for item in sorted_results[:top_k]
        ]

    async def _rrf_retrieve(
        self, query: str, top_k: int, module_ids: tuple[str, ...]
    ) -> list[dict[str, Any]]:
        """Reciprocal Rank Fusion (RRF) retrieval."""
        dense_task = asyncio.create_task(self._dense_retrieve(query, top_k * 2, module_ids))
        sparse_task = asyncio.create_task(self._sparse_retrieve(query, top_k * 2))
        semantic_task = asyncio.create_task(self._semantic_retrieve(query, top_k * 2, module_ids))

        dense_results, sparse_results, semantic_results = await asyncio.gather(
            dense_task, sparse_task, semantic_task, return_exceptions=True
        )

        if isinstance(dense_results, Exception):
            dense_results = []
        if isinstance(sparse_results, Exception):
            sparse_results = []
        if isinstance(semantic_results, Exception):
            semantic_results = []

        # RRF scoring: 1 / (k + rank)
        k = 60
        scores = {}

        for rank, r in enumerate(dense_results, 1):
            key = r["id"]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)

        for rank, r in enumerate(sparse_results, 1):
            key = r["id"]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)

        for rank, r in enumerate(semantic_results, 1):
            key = r["id"]
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)

        # Get full result objects
        all_results = {r["id"]: r for r in dense_results + sparse_results + semantic_results}

        sorted_ids = sorted(scores.keys(), key=lambda x: scores[x], reverse=True)

        return [
            {
                **all_results[doc_id],
                "score": scores[doc_id],
                "source": "rrf",
            }
            for doc_id in sorted_ids[:top_k]
            if doc_id in all_results
        ]


__all__ = ["HybridRetriever", "RetrievalResult"]