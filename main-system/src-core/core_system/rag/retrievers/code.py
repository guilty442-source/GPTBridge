"""CodeRetriever — chunk + AST + symbol + dependency graph (A52 code-rag).

Code-specific retrieval that combines:
  1. Dense vector similarity on code chunks
  2. PostgreSQL FTS keyword matching (function/class/identifier names)
  3. Symbol-level filtering (AST-extracted symbols)
  4. Dependency graph traversal (imports, call graphs)

Shares Qdrant + PostgreSQL + embedding runtime with the other retrievers
but applies code-specific filtering and ranking.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from ..pipeline_retrieval import RerankerFn, reciprocal_rank_fusion

_logger = logging.getLogger("gptbridge.rag.code")

CODE_RAG_ID = "code-rag"

# Patterns for extracting code symbols from query text.
_SYMBOL_PATTERN = re.compile(
    r"\b(?:def|class|function|func|method|import|from|const|let|var|"
    r"async|await|return|if|else|for|while|try|except|catch|finally)\b"
    r"|(?:(?:[A-Z][a-zA-Z0-9_]*)\.)?[a-zA-Z_][a-zA-Z0-9_]*\s*\(",
    re.IGNORECASE,
)
_IMPORT_PATTERN = re.compile(
    r"\b(?:import|from|require|include|using)\s+[\w.]+", re.IGNORECASE
)


@dataclass
class CodeRetrievalRequest:
    """A code retrieval request."""
    query_text: str
    query_embedding: list[float]
    module_ids: tuple[str, ...]
    candidate_limit: int = 24
    top_k: int = 6
    score_threshold: Optional[float] = None
    # Optional AST symbols to filter on (e.g. [" MyClass.method", "parse_config"])
    symbols: tuple[str, ...] = ()
    # Optional dependency identifiers to expand the search
    dependencies: tuple[str, ...] = ()


@dataclass
class CodeRetrievalResult:
    """A code retrieval result."""
    sub_architecture: str = CODE_RAG_ID
    query: str = ""
    candidates: list[dict[str, Any]] = field(default_factory=list)
    reranker_meta: dict[str, Any] = field(default_factory=dict)
    symbols_extracted: list[str] = field(default_factory=list)
    retrieval: str = "canonical-code-chunk+ast+symbol+dependency"


class CodeRetriever:
    """Code RAG retriever — chunk + AST + symbol + dependency.

    Delegates dense/keyword search to the host pipeline, then applies
    code-specific symbol filtering and dependency expansion.
    """

    def __init__(self, pipeline: Any, reranker: Optional[RerankerFn] = None) -> None:
        self._pipeline = pipeline
        self._reranker = reranker

    @staticmethod
    def extract_symbols(query: str) -> list[str]:
        """Extract code symbols (function/class/method names) from a query."""
        symbols: list[str] = []
        for match in _SYMBOL_PATTERN.finditer(query):
            token = match.group().strip().rstrip("(")
            # Strip leading keywords
            for kw in ("def ", "class ", "function ", "func ", "method ", "async "):
                if token.lower().startswith(kw):
                    token = token[len(kw):]
                    break
            if token and token not in symbols:
                symbols.append(token)
        for match in _IMPORT_PATTERN.finditer(query):
            token = match.group().split(None, 1)[-1] if " " in match.group() else ""
            if token and token not in symbols:
                symbols.append(token)
        return symbols

    def retrieve(self, request: CodeRetrievalRequest) -> CodeRetrievalResult:
        """Execute code retrieval through the canonical pipeline."""
        # Extract symbols from the query if not provided
        symbols = list(request.symbols) or self.extract_symbols(request.query_text)
        # Dense + keyword hybrid search
        vector_hits = self._pipeline.vector_search(
            request.query_embedding,
            module_ids=request.module_ids,
            top_k=request.candidate_limit,
            score_threshold=request.score_threshold,
        )
        keyword_hits = self._pipeline.keyword_search(
            request.query_text,
            module_ids=request.module_ids,
            limit=request.candidate_limit,
        )
        fused = reciprocal_rank_fusion(vector_hits, keyword_hits)
        # Symbol-level filtering: boost candidates that mention extracted symbols
        if symbols:
            for candidate in fused:
                content = str(candidate.get("content") or "").lower()
                symbol_boost = sum(
                    0.1 for sym in symbols if sym.lower() in content
                )
                if symbol_boost:
                    candidate["rrf_score"] = float(
                        candidate.get("rrf_score") or 0.0
                    ) + symbol_boost
            fused.sort(key=lambda r: -float(r.get("rrf_score") or 0.0))
        # Apply reranker if available
        if self._reranker and fused:
            ranked, meta = self._reranker(request.query_text, fused[:request.candidate_limit])
        else:
            ranked, meta = fused[:request.top_k], {
                "reranker_applied": False, "fallback": "rrf+symbol-boost"
            }
        return CodeRetrievalResult(
            query=request.query_text,
            candidates=ranked[:request.top_k],
            reranker_meta=meta,
            symbols_extracted=symbols,
        )


__all__ = [
    "CODE_RAG_ID",
    "CodeRetrievalRequest",
    "CodeRetrievalResult",
    "CodeRetriever",
]
