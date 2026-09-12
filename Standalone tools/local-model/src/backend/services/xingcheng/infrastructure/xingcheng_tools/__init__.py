"""星澄工具與知識取得層 (XingCheng Tools & Knowledge Acquisition Layer)。

與 `native_transformer/`（模型核心）嚴格分離。本套件負責：
  - Tool Router（統一工具路由）
  - Intent Router（搜尋意圖判斷）
  - Web Search（SearXNG + 可插拔 Provider）
  - Web Fetch（安全受控的網頁擷取）
  - HTML / Text Parser（乾淨文本萃取）
  - Content Cleaner / Chunking / Deduplication
  - Reranker（Keyword + Semantic + Source Quality + Freshness）
  - Unified RAG Retrieval Interface（Local RAG + Web Search 共用）
  - Context Builder（來源標記 + 動態組合）
  - Source Metadata（來源追蹤）
  - Web Cache（TTL by 資料型態）

架構原則：
  1. 模型核心與網路功能分離 — 網路搜尋不改變星澄原生模型核心。
  2. 搜尋 Provider 可替換 — 上層不依賴單一搜尋引擎 API 格式。
  3. RAG 與 Web Search 共用統一 Retrieval Interface。
  4. 網路結果必須有來源 — 所有資料保存 Source Metadata。
  5. 網路內容不得直接控制模型 — 防止 Prompt Injection / SSRF / 惡意內容。
  6. 所有搜尋、解析、排序與 Context 建構優先在本地完成。

正式流程：
  星澄 → Intent Router → Tool Router
  Tool Router → Local SQL / Local RAG / Web Search / Git / File / Shell
  Web Search → SearXNG → Results → Fetcher → Parser → Cleaner
    → Dedup → Reranker → Web RAG → Context Builder → 星澄模型 → 回答
"""

from __future__ import annotations

from .source import SourceMetadata, SourceType
from .intent_router import IntentRouter, IntentResult, SearchIntent
from .tool_router import ToolRouter, ToolRoute, ToolType
from .search.types import (
    SearchRequest, SearchResponse, SearchStatus,
    SearchResult, FetchStatus, SearchPriority,
)
from .search.provider import SearchProvider, SearchProviderError
from .search.searxng import SearXNGProvider
from .search.provider_manager import ProviderManager
from .search.cache import WebCache, CacheEntry, CacheTier
from .search.query import QueryBuilder, QueryPlan
from .search.prefilter import PreFilter, PreFilterConfig
from .fetch.safety import URLSafetyChecker, URLSafetyError, URLSafetyConfig
from .fetch.fetcher import WebFetcher, FetchResult, FetchConfig
from .parser.html_parser import HTMLParser, ParsedPage
from .parser.cleaner import ContentCleaner, CleanDocument
from .chunking import Chunker, TextChunk
from .dedup import Deduplicator, DedupResult
from .reranker import Reranker, RerankResult, RerankerConfig
from .evidence_selector import EvidenceSelector, Evidence, EvidenceSelectionResult
from .rag.retrieval import (
    UnifiedRetriever, RetrievalRequest, RetrievalResult,
    Retriever, SourceRouter,
)
from .rag.context_builder import ContextBuilder, ContextEntry, BuiltContext
from .citation import CitationResolver, SourceFormatter, Citation, CitationResult
from .response_validator import ResponseValidator, ValidationResult
from .trace import SearchTrace, TraceEvent
from .pipeline import WebSearchPipeline, PipelineConfig, PipelineResult

__version__ = "1.00000"

__all__ = [
    # Source
    "SourceMetadata",
    "SourceType",
    # Intent
    "IntentRouter",
    "IntentResult",
    "SearchIntent",
    # Tool Router
    "ToolRouter",
    "ToolRoute",
    "ToolType",
    # Search types
    "SearchRequest",
    "SearchResponse",
    "SearchStatus",
    "SearchResult",
    "FetchStatus",
    "SearchPriority",
    # Search Provider
    "SearchProvider",
    "SearchProviderError",
    "SearXNGProvider",
    "ProviderManager",
    # Cache
    "WebCache",
    "CacheEntry",
    "CacheTier",
    # Query
    "QueryBuilder",
    "QueryPlan",
    # PreFilter
    "PreFilter",
    "PreFilterConfig",
    # Fetch
    "URLSafetyChecker",
    "URLSafetyError",
    "URLSafetyConfig",
    "WebFetcher",
    "FetchResult",
    "FetchConfig",
    # Parser
    "HTMLParser",
    "ParsedPage",
    "ContentCleaner",
    "CleanDocument",
    # Chunking
    "Chunker",
    "TextChunk",
    # Dedup
    "Deduplicator",
    "DedupResult",
    # Reranker
    "Reranker",
    "RerankResult",
    "RerankerConfig",
    # Evidence
    "EvidenceSelector",
    "Evidence",
    "EvidenceSelectionResult",
    # RAG
    "UnifiedRetriever",
    "RetrievalRequest",
    "RetrievalResult",
    "Retriever",
    "SourceRouter",
    # Context
    "ContextBuilder",
    "ContextEntry",
    "BuiltContext",
    # Citation
    "CitationResolver",
    "SourceFormatter",
    "Citation",
    "CitationResult",
    # Validation
    "ResponseValidator",
    "ValidationResult",
    # Trace
    "SearchTrace",
    "TraceEvent",
    # Pipeline
    "WebSearchPipeline",
    "PipelineConfig",
    "PipelineResult",
]
