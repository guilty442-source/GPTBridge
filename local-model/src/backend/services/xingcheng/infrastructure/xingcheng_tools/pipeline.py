"""Web Search Pipeline — 完整搜尋工具執行流程。

對應需求 21, 45：
  使用者問題 → Intent Router → Tool Router → Query Planner → Cache
  → Search Provider Manager → SearXNG / Provider → Result Normalizer
  → Pre-Filter → Fetch Queue → Web Fetcher → Parser → Cleaner
  → Chunker → Deduplication → Reranker → Evidence Selector
  → Context Builder → 星澄原生模型 → Answer → Citation Resolver
  → Response Validator → 使用者

對應需求 44（降級策略）：
  任何子模組失敗時採降級策略，不得因單一工具失效造成整個星澄無法回答。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .citation import CitationResolver, SourceFormatter
from .chunking import Chunker, TextChunk
from .dedup import Deduplicator
from .evidence_selector import EvidenceSelector, Evidence
from .fetch.fetcher import WebFetcher, FetchConfig
from .fetch.safety import URLSafetyChecker
from .intent_router import IntentRouter, SearchIntent
from .parser.cleaner import ContentCleaner
from .parser.html_parser import HTMLParser
from .rag.context_builder import ContextBuilder, BuiltContext, ContextEntry
from .reranker import Reranker, RerankResult
from .response_validator import ResponseValidator
from .search.cache import WebCache, CacheTier
from .search.prefilter import PreFilter
from .search.provider_manager import ProviderManager
from .search.query import QueryBuilder
from .search.types import (
    FetchStatus,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SearchStatus,
)
from .source import SourceMetadata, SourceType
from .trace import SearchTrace
from .tool_router import ToolRouter, ToolType

log = logging.getLogger(__name__)


@dataclass
class PipelineConfig:
    """Pipeline 設定。"""

    max_fetch_concurrent: int = 4
    max_results: int = 10
    max_chunks: int = 20
    token_budget: int = 2048
    chunk_size: int = 512
    chunk_overlap: int = 64
    enable_cache: bool = True
    enable_reranker: bool = True
    enable_dedup: bool = True
    enable_prefilter: bool = True
    searxng_url: str = "http://127.0.0.1:8080"
    fetch_timeout: float = 15.0
    fetch_max_bytes: int = 5 * 1024 * 1024


@dataclass
class PipelineResult:
    """Pipeline 執行結果。"""

    response: SearchResponse
    context: BuiltContext | None = None
    evidence: list[Evidence] = field(default_factory=list)
    source_metadata: list[SourceMetadata] = field(default_factory=list)
    trace: SearchTrace | None = None
    answer: str = ""
    validated_answer: str = ""
    validation: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "response": self.response.to_dict(),
            "context": self.context.to_dict() if self.context else None,
            "evidence_count": len(self.evidence),
            "source_count": len(self.source_metadata),
            "answer": self.answer,
            "validated_answer": self.validated_answer,
            "validation": self.validation,
            "trace": self.trace.summary() if self.trace else None,
        }


class WebSearchPipeline:
    """完整網路搜尋管線。"""

    def __init__(
        self,
        config: PipelineConfig | None = None,
        *,
        provider_manager: ProviderManager | None = None,
        fetcher: WebFetcher | None = None,
        parser: HTMLParser | None = None,
        cleaner: ContentCleaner | None = None,
        chunker: Chunker | None = None,
        deduplicator: Deduplicator | None = None,
        reranker: Reranker | None = None,
        evidence_selector: EvidenceSelector | None = None,
        context_builder: ContextBuilder | None = None,
        cache: WebCache | None = None,
        intent_router: IntentRouter | None = None,
        tool_router: ToolRouter | None = None,
        citation_resolver: CitationResolver | None = None,
        source_formatter: SourceFormatter | None = None,
        response_validator: ResponseValidator | None = None,
    ) -> None:
        self.config = config or PipelineConfig()
        self.safety = URLSafetyChecker()
        self.provider_manager = provider_manager or self._default_provider_manager()
        self.fetcher = fetcher or WebFetcher(
            FetchConfig(timeout=self.config.fetch_timeout, max_bytes=self.config.fetch_max_bytes),
            safety=self.safety,
        )
        self.parser = parser or HTMLParser()
        self.cleaner = cleaner or ContentCleaner()
        self.chunker = chunker or Chunker(
            chunk_size=self.config.chunk_size,
            chunk_overlap=self.config.chunk_overlap,
        )
        self.deduplicator = deduplicator or Deduplicator()
        self.reranker = reranker or Reranker()
        self.evidence_selector = evidence_selector or EvidenceSelector(
            token_budget=self.config.token_budget
        )
        self.context_builder = context_builder or ContextBuilder()
        self.cache = cache or WebCache()
        self.intent_router = intent_router or IntentRouter()
        self.tool_router = tool_router or ToolRouter(self.intent_router)
        self.query_builder = QueryBuilder()
        self.citation_resolver = citation_resolver or CitationResolver()
        self.source_formatter = source_formatter or SourceFormatter()
        self.response_validator = response_validator or ResponseValidator()
        self._pre_filter = PreFilter(safety=self.safety)

    def _default_provider_manager(self) -> ProviderManager:
        from .search.searxng import SearXNGProvider
        provider = SearXNGProvider(base_url=self.config.searxng_url)
        return ProviderManager([provider])

    # ── 主流程 ──────────────────────────────────────────────────
    def search(
        self,
        question: str,
        *,
        language: str = "zh-TW",
        max_results: int | None = None,
        time_range: str | None = None,
    ) -> PipelineResult:
        """執行完整搜尋管線。"""
        trace = SearchTrace(request_id="")
        start = time.time()

        # 1. Intent Router
        t0 = time.time()
        intent = self.intent_router.classify(question)
        trace.add_event("intent_router", duration=time.time() - t0,
                        detail=intent.to_dict())

        # 如果不需要搜尋，直接回傳
        if not intent.needs_web_search:
            response = SearchResponse(
                request_id="",
                status=SearchStatus.SUCCESS,
                queries=[],
                execution_time=time.time() - start,
            )
            trace.finish()
            return PipelineResult(response=response, trace=trace)

        # 2. Tool Router
        t0 = time.time()
        route = self.tool_router.route(question)
        trace.add_event("tool_router", duration=time.time() - t0,
                        detail=route.to_dict())

        # 3. Query Planner
        t0 = time.time()
        request = self.query_builder.build_request(
            question,
            language=language,
            max_results=max_results or self.config.max_results,
            time_range=time_range,
        )
        trace.request_id = request.request_id
        trace.add_event("query_planner", duration=time.time() - t0,
                        detail={"queries": request.queries, "strategy": request.extra.get("query_plan", {}).get("strategy")})

        # 4. Cache Lookup
        cache_hits = 0
        cached_results: list[SearchResult] = []
        if self.config.enable_cache:
            t0 = time.time()
            for q in request.queries:
                key = WebCache.make_key(q, prefix="search")
                entry = self.cache.get(key)
                if entry is not None:
                    cached_results.extend(entry.value)
                    cache_hits += 1
            trace.add_event("cache_lookup", duration=time.time() - t0,
                            detail={"hits": cache_hits, "cached_results": len(cached_results)})

        # 5. Search Provider Manager
        t0 = time.time()
        fresh_results: list[SearchResult] = []
        provider_errors: list[str] = []
        if cache_hits < len(request.queries):
            fresh_results, provider_errors = self.provider_manager.search(request)
            # 存入 cache
            if self.config.enable_cache and fresh_results:
                for q in request.queries:
                    key = WebCache.make_key(q, prefix="search")
                    q_results = [r for r in fresh_results if r.search_query == q]
                    if q_results:
                        self.cache.put(key, q_results, CacheTier.SEARCH_RESULT)
        trace.add_event("search_provider", duration=time.time() - t0,
                        detail={"fresh_results": len(fresh_results), "errors": provider_errors},
                        error=provider_errors[0] if provider_errors else None)

        all_results = cached_results + fresh_results

        if not all_results:
            response = SearchResponse(
                request_id=request.request_id,
                status=SearchStatus.NO_RESULT if not provider_errors else SearchStatus.PROVIDER_ERROR,
                queries=request.queries,
                errors=provider_errors,
                execution_time=time.time() - start,
                cache_hits=cache_hits,
            )
            trace.finish()
            return PipelineResult(response=response, trace=trace)

        # 6. Pre-Filter
        t0 = time.time()
        if self.config.enable_prefilter:
            all_results, dropped = self._pre_filter.filter(all_results, request.queries)
            trace.add_event("pre_filter", duration=time.time() - t0,
                            detail={"remaining": len(all_results), "dropped": len(dropped)})
        else:
            trace.add_event("pre_filter", duration=time.time() - t0,
                            detail={"skipped": True})

        # 7. Web Fetch
        t0 = time.time()
        fetched = self._fetch_results(all_results)
        fetched_count = sum(1 for r in fetched if r.fetch_status == FetchStatus.SUCCESS)
        trace.add_event("web_fetch", duration=time.time() - t0,
                        detail={"fetched": fetched_count, "total": len(fetched)})

        # 8. Parser + Cleaner + Chunker
        t0 = time.time()
        chunks = self._parse_and_chunk(fetched, request)
        trace.add_event("parse_clean_chunk", duration=time.time() - t0,
                        detail={"chunk_count": len(chunks)})

        # 9. Deduplication
        t0 = time.time()
        cross_validation: list[dict[str, Any]] = []
        if self.config.enable_dedup and chunks:
            dedup_result = self.deduplicator.deduplicate(chunks)
            chunks = dedup_result.unique
            cross_validation = dedup_result.cross_validation
            trace.add_event("dedup", duration=time.time() - t0,
                            detail={"unique": len(chunks), "duplicates": len(dedup_result.duplicates),
                                   "cross_validations": len(cross_validation)})
        else:
            trace.add_event("dedup", duration=time.time() - t0, detail={"skipped": True})

        # 10. Reranker
        t0 = time.time()
        ranked: list[RerankResult] = []
        if self.config.enable_reranker and chunks:
            # 用第一個 query 做 rerank（或合併所有 query）
            combined_query = " ".join(request.queries[:2])
            ranked = self.reranker.rerank(chunks, combined_query, max_results=self.config.max_chunks)
            trace.add_event("reranker", duration=time.time() - t0,
                            detail={"ranked": len(ranked)})
        else:
            # 不用 reranker，直接用原始順序
            ranked = [RerankResult(chunk=c, score=0.5) for c in chunks]
            trace.add_event("reranker", duration=time.time() - t0, detail={"skipped": True})

        # 11. Evidence Selector
        t0 = time.time()
        selection = self.evidence_selector.select(
            ranked,
            token_budget=self.config.token_budget,
            cross_validation=cross_validation,
        )
        trace.add_event("evidence_selector", duration=time.time() - t0,
                        detail={"selected": len(selection.evidence), "tokens": selection.total_tokens,
                               "skipped": selection.skipped})

        # 12. Source Metadata
        source_metadata = self._build_source_metadata(selection.evidence, request)

        # 13. Context Builder
        t0 = time.time()
        context = self.context_builder.build(
            user_question=question,
            web_evidence=selection.evidence,
            source_metadata=source_metadata,
        )
        trace.add_event("context_builder", duration=time.time() - t0,
                        detail=context.to_dict())

        # 14. 決定 status
        if fetched_count > 0 and selection.evidence:
            status = SearchStatus.SUCCESS if not provider_errors else SearchStatus.PARTIAL_SUCCESS
        elif fetched_count > 0:
            status = SearchStatus.PARTIAL_SUCCESS
        else:
            status = SearchStatus.FETCH_ERROR if not provider_errors else SearchStatus.PROVIDER_ERROR

        response = SearchResponse(
            request_id=request.request_id,
            status=status,
            queries=request.queries,
            result_count=len(all_results),
            fetched_count=fetched_count,
            valid_source_count=len(source_metadata),
            evidence_count=len(selection.evidence),
            selected_sources=[e.to_dict() for e in selection.evidence],
            evidence_text=context.full_text,
            source_metadata=[m.to_dict() for m in source_metadata],
            execution_time=time.time() - start,
            errors=provider_errors,
            cache_hits=cache_hits,
        )

        trace.finish()
        return PipelineResult(
            response=response,
            context=context,
            evidence=selection.evidence,
            source_metadata=source_metadata,
            trace=trace,
        )

    # ── 最終回答流程 (需求 36, 40) ───────────────────────────────
    def generate_answer(
        self,
        pipeline_result: PipelineResult,
        model_answer: str,
        *,
        user_question: str = "",
    ) -> dict[str, Any]:
        """模型回答後的後處理：Citation → Source Formatter → Response Validator。"""
        trace = pipeline_result.trace or SearchTrace(request_id="")

        # Citation Resolver
        t0 = time.time()
        citation_result = self.citation_resolver.resolve(
            model_answer, pipeline_result.source_metadata
        )
        trace.add_event("citation_resolver", duration=time.time() - t0,
                        detail={"citations": len(citation_result.citations),
                               "unresolved": len(citation_result.unresolved_references)})

        # Source Formatter
        t0 = time.time()
        formatted = self.source_formatter.format(citation_result)
        trace.add_event("source_formatter", duration=time.time() - t0)

        # Response Validator
        t0 = time.time()
        validation = self.response_validator.validate(
            formatted,
            citation_result,
            user_question=user_question,
            search_errors=pipeline_result.response.errors,
        )
        trace.add_event("response_validator", duration=time.time() - t0,
                        detail=validation.to_dict())

        return {
            "answer": validation.sanitized_answer,
            "citations": citation_result.to_dict(),
            "validation": validation.to_dict(),
            "trace": trace.summary(),
        }

    # ── 內部方法 ────────────────────────────────────────────────
    def _fetch_results(self, results: list[SearchResult]) -> list[SearchResult]:
        """批次擷取搜尋結果的網頁內容。"""
        fetched: list[SearchResult] = []
        for result in results:
            if not self.config.enable_cache:
                fetch_result = self.fetcher.fetch(result.url)
            else:
                cache_key = WebCache.make_key(result.url, prefix="fetch")
                entry = self.cache.get(cache_key)
                if entry is not None:
                    result.fetch_status = FetchStatus.SUCCESS
                    result.raw["fetched_content"] = entry.value
                    fetched.append(result)
                    continue
                fetch_result = self.fetcher.fetch(result.url)

            if fetch_result.ok:
                result.fetch_status = FetchStatus.SUCCESS
                result.raw["fetched_content"] = fetch_result.text()
                result.raw["content_type"] = fetch_result.content_type
                result.raw["final_url"] = fetch_result.final_url
                # 存入 cache
                if self.config.enable_cache:
                    tier = self.cache.guess_tier(result.url, fetch_result.content_type)
                    self.cache.put(
                        WebCache.make_key(result.url, prefix="fetch"),
                        fetch_result.text(),
                        tier,
                    )
            elif "timeout" in (fetch_result.error or "").lower():
                result.fetch_status = FetchStatus.TIMEOUT
            elif "Content-Type" in (fetch_result.error or ""):
                result.fetch_status = FetchStatus.UNSUPPORTED
            elif "安全" in (fetch_result.error or ""):
                result.fetch_status = FetchStatus.BLOCKED
            else:
                result.fetch_status = FetchStatus.FAILED
            fetched.append(result)
        return fetched

    def _parse_and_chunk(
        self,
        results: list[SearchResult],
        request: SearchRequest,
    ) -> list[TextChunk]:
        """解析網頁 → 清理 → 切分。"""
        all_chunks: list[TextChunk] = []
        for result in results:
            if result.fetch_status != FetchStatus.SUCCESS:
                continue
            html = result.raw.get("fetched_content", "")
            if not html:
                continue

            try:
                page = self.parser.parse(html, result.url)
                doc = self.cleaner.clean(page)
                if doc.word_count < 30:
                    continue
                chunks = self.chunker.chunk(doc)
                all_chunks.extend(chunks)
            except Exception as exc:
                log.warning("parse/chunk failed for %s: %s", result.url, exc)

        return all_chunks

    def _build_source_metadata(
        self,
        evidence: list[Evidence],
        request: SearchRequest,
    ) -> list[SourceMetadata]:
        """從選出的 evidence 建立 SourceMetadata。"""
        metadata: list[SourceMetadata] = []
        for ev in evidence:
            chunk = ev.chunk
            meta = SourceMetadata.for_web(
                url=chunk.url,
                title=chunk.title,
                search_query=request.queries[0] if request.queries else "",
                provider="searxng",
                published_time=chunk.published_time,
                relevance_score=ev.score,
            )
            meta.chunk_id = chunk.chunk_id
            metadata.append(meta)
        return metadata


__all__ = ["WebSearchPipeline", "PipelineConfig", "PipelineResult"]
