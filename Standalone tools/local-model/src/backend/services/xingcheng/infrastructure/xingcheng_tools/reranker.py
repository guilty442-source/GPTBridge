"""Reranker — 搜尋結果重排序。

對應需求 33：
  評分因素：Query Relevance、Semantic Similarity、Keyword Match、
  Source Quality、Freshness、Content Completeness、Duplicate Penalty
  不得只依搜尋引擎原始排名。
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any

from .chunking import TextChunk


# 可信任來源
_TRUSTED_DOMAINS: dict[str, float] = {
    "wikipedia.org": 0.9,
    "github.com": 0.85,
    "stackoverflow.com": 0.8,
    "arxiv.org": 0.9,
    "docs.python.org": 0.9,
    "pytorch.org": 0.9,
    "developer.mozilla.org": 0.85,
    "official.microsoft.com": 0.9,
}


@dataclass
class RerankResult:
    """重排序結果。"""

    chunk: TextChunk
    score: float
    scores: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk.chunk_id,
            "url": self.chunk.url,
            "title": self.chunk.title,
            "total_score": round(self.score, 4),
            "scores": {k: round(v, 4) for k, v in self.scores.items()},
        }


@dataclass
class RerankerConfig:
    """Reranker 權重設定。"""

    weight_keyword: float = 0.25
    weight_semantic: float = 0.20
    weight_source_quality: float = 0.20
    weight_freshness: float = 0.15
    weight_completeness: float = 0.15
    weight_duplicate_penalty: float = 0.05
    freshness_decay_hours: float = 168.0  # 一週半衰期


class Reranker:
    """搜尋結果重排序器。"""

    def __init__(self, config: RerankerConfig | None = None) -> None:
        self.config = config or RerankerConfig()

    def rerank(
        self,
        chunks: list[TextChunk],
        query: str,
        *,
        max_results: int = 10,
    ) -> list[RerankResult]:
        """對 chunks 重新排序。"""
        if not chunks:
            return []

        results: list[RerankResult] = []
        for chunk in chunks:
            scores = self._score_chunk(chunk, query)
            total = sum(scores.values())
            results.append(RerankResult(chunk=chunk, score=total, scores=scores))

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:max_results]

    def _score_chunk(self, chunk: TextChunk, query: str) -> dict[str, float]:
        cfg = self.config
        scores: dict[str, float] = {}

        # Keyword Match
        scores["keyword"] = cfg.weight_keyword * self._keyword_score(chunk, query)

        # Semantic Similarity（簡易：詞重疊率）
        scores["semantic"] = cfg.weight_semantic * self._semantic_score(chunk, query)

        # Source Quality
        scores["source_quality"] = cfg.weight_source_quality * self._source_quality_score(chunk)

        # Freshness
        scores["freshness"] = cfg.weight_freshness * self._freshness_score(chunk)

        # Content Completeness
        scores["completeness"] = cfg.weight_completeness * self._completeness_score(chunk)

        # Duplicate Penalty（預設 0，由 Deduplicator 處理）
        scores["duplicate_penalty"] = 0.0

        return scores

    @staticmethod
    def _keyword_score(chunk: TextChunk, query: str) -> float:
        query_terms = set(re.findall(r"\w+", query.lower()))
        if not query_terms:
            return 0.0
        text = (chunk.title + " " + chunk.text).lower()
        text_terms = set(re.findall(r"\w+", text))
        overlap = len(query_terms & text_terms)
        return min(1.0, overlap / max(1, len(query_terms)))

    @staticmethod
    def _semantic_score(chunk: TextChunk, query: str) -> float:
        """簡易語意相似度：bi-gram 重疊率。"""
        query_bigrams = set()
        for word in re.findall(r"[\u3400-\u9fff]+", query):
            for i in range(len(word) - 1):
                query_bigrams.add(word[i : i + 2])
        if not query_bigrams:
            return 0.0
        text = chunk.title + " " + chunk.text
        text_bigrams = set()
        for word in re.findall(r"[\u3400-\u9fff]+", text):
            for i in range(len(word) - 1):
                text_bigrams.add(word[i : i + 2])
        if not text_bigrams:
            return 0.0
        return len(query_bigrams & text_bigrams) / len(query_bigrams)

    @staticmethod
    def _source_quality_score(chunk: TextChunk) -> float:
        return _TRUSTED_DOMAINS.get(chunk.domain, 0.5)

    def _freshness_score(self, chunk: TextChunk) -> float:
        if not chunk.published_time:
            return 0.3  # 未知時間給中等偏低分
        try:
            # 嘗試解析 ISO 格式時間
            from datetime import datetime
            # 處理多種格式
            ts = chunk.published_time.strip()
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d",
                        "%Y/%m/%d", "%Y年%m月%d日"):
                try:
                    dt = datetime.strptime(ts[:20], fmt)
                    age_hours = (time.time() - dt.timestamp()) / 3600
                    decay = math.exp(-age_hours / self.config.freshness_decay_hours)
                    return max(0.0, min(1.0, decay))
                except (ValueError, OSError):
                    continue
        except Exception:
            pass
        return 0.3

    @staticmethod
    def _completeness_score(chunk: TextChunk) -> float:
        """依 token estimate 評估內容完整度。"""
        tokens = chunk.token_estimate
        if tokens < 50:
            return 0.2
        if tokens < 200:
            return 0.5
        if tokens < 500:
            return 0.8
        return 1.0


__all__ = ["Reranker", "RerankResult", "RerankerConfig"]
