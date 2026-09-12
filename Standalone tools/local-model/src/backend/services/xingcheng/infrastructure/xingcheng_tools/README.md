# 星澄工具與知識取得層 (XingCheng Tools & Knowledge Acquisition)

「星澄」獨立的網路搜尋與網頁知識取得能力 — 與模型核心嚴格分離。

## 架構分離原則

```
星澄模型核心（native_transformer/）
  → Python → PyTorch → Transformer → Tensor → Computation Graph
  → ATen / C++ Backend → cuBLASLt / cuDNN / FlashAttention
  → Triton / Gluon / CUDA → GPU / CPU

星澄工具與知識取得層（xingcheng_tools/ 本套件）
  → Tool Router → Web Search → Browser / Web Fetch
  → HTML / Text Parser → Content Cleaner → Deduplication
  → Reranker → RAG → Context Builder → 星澄模型
```

**網路搜尋只作為星澄的外部知識取得能力，不改變星澄原生模型核心。**

## 完整執行流程（需求 21, 45）

```
使用者問題
  → Intent Router（判斷是否需要即時／外部資料）
  → Tool Router（建立 Search Request）
  → Query Planner（問題拆解 → 關鍵字抽取 → Query Rewrite → 多組 Query）
  → Cache Lookup（TTL by 資料型態）
  → Search Provider Manager（SearXNG / Fallback Provider）
  → Result Normalizer（統一 SearchResult 格式）
  → Pre-Filter（URL 去重 / 黑名單 / 廣告 / 登入牆 / 無關結果）
  → Fetch Queue（平行但有限制的 Web Fetch）
  → Web Fetcher（Timeout / Redirect / Content-Type / Retry / 大小限制）
  → HTML Parser（DOM Parse → Main Content Detection → Metadata Extraction）
  → Content Cleaner（移除 Nav / Footer / Ads / Cookie / Script / Style）
  → Chunker（切分為 Chunk，保留 URL / Title / Domain / Published / Position）
  → Deduplication（URL → Hash → Near Duplicate → Semantic Duplicate）
  → Reranker（Keyword + Semantic + Source Quality + Freshness + Completeness）
  → Evidence Selector（Token Budget Check → Final Evidence Set）
  → Context Builder（System → User → SQL → Local RAG → Web Evidence → Metadata）
  → 星澄原生模型（推理 → 衝突偵測 → Answer Draft）
  → Citation Resolver（Source ID → URL / Title / Domain）
  → Source Formatter（統一產生引用）
  → Response Validator（Prompt Injection / 來源驗證 / 時間衝突檢查）
  → 使用者
```

## 套件結構

```
xingcheng_tools/
├── __init__.py              # 公開 API（全部模組統一匯出）
├── source.py                # SourceMetadata / SourceType（來源追蹤）
├── intent_router.py         # IntentRouter（搜尋意圖判斷）
├── tool_router.py           # ToolRouter（統一工具路由：LLM/SQL/RAG/Web/Git/File/Shell）
├── trace.py                 # SearchTrace（內部執行紀錄，與對話內容分離）
├── evidence_selector.py     # EvidenceSelector（Token Budget → Final Evidence Set）
├── citation.py              # CitationResolver + SourceFormatter（引用映射與格式化）
├── response_validator.py    # ResponseValidator（Prompt Injection / 來源 / 時間衝突檢查）
├── chunking.py              # Chunker / TextChunk（內容切分 + Metadata 保留）
├── dedup.py                 # Deduplicator（URL → Hash → Near Duplicate → Semantic）
├── reranker.py              # Reranker（Keyword + Semantic + Source + Freshness + Completeness）
├── pipeline.py              # WebSearchPipeline（完整管線編排 + 降級策略）
├── search/
│   ├── types.py             #   SearchRequest / SearchResponse / SearchStatus / FetchStatus
│   ├── query.py             #   QueryBuilder / QueryPlan（Query Planner）
│   ├── provider.py          #   SearchProvider 抽象介面
│   ├── searxng.py           #   SearXNGProvider（主要搜尋聚合入口）
│   ├── provider_manager.py  #   ProviderManager（fallback 機制）
│   ├── cache.py             #   WebCache / CacheTier（TTL by 資料型態）
│   └── prefilter.py         #   PreFilter（初篩：URL/黑名單/廣告/登入牆/無關結果）
├── fetch/
│   ├── safety.py            #   URLSafetyChecker（SSRF 防護 / 私有網段 / 危險 scheme）
│   └── fetcher.py           #   WebFetcher（Timeout / Redirect / Content-Type / Retry）
├── parser/
│   ├── html_parser.py       #   HTMLParser（DOM Parse → Main Content → Metadata）
│   └── cleaner.py           #   ContentCleaner（移除 Nav/Footer/Ads/Cookie/Script）
├── rag/
│   ├── retrieval.py         #   UnifiedRetriever / SourceRouter（統一檢索介面）
│   └── context_builder.py   #   ContextBuilder（動態順序組合 + 來源標記）
└── tests/
    └── test_tools.py        # 41 項端對端測試
```

## 核心設計對應

| 需求 | 實作 | 檔案 |
|------|------|------|
| 1. 搜尋意圖判斷 | IntentRouter | `intent_router.py` |
| 2. 搜尋查詢產生 | QueryBuilder / QueryPlan | `search/query.py` |
| 3. Search Provider 層 | SearchProvider + SearXNGProvider | `search/provider.py`, `searxng.py` |
| 4. 網頁取得層 | WebFetcher | `fetch/fetcher.py` |
| 5. 網頁解析 | HTMLParser | `parser/html_parser.py` |
| 6. 內容切分 | Chunker / TextChunk | `chunking.py` |
| 7. 去重 | Deduplicator | `dedup.py` |
| 8. Reranker | Reranker | `reranker.py` |
| 9. RAG 整合 | UnifiedRetriever / SourceRouter | `rag/retrieval.py` |
| 10. Context Builder | ContextBuilder | `rag/context_builder.py` |
| 11. 來源追蹤 | SourceMetadata | `source.py` |
| 12. 時效性 | IntentRouter 時效性關鍵詞 | `intent_router.py` |
| 13. 快取 | WebCache / CacheTier | `search/cache.py` |
| 14. 安全限制 | URLSafetyChecker | `fetch/safety.py` |
| 15. 本地優先 | 全套件本地處理 | — |
| 16. RAG 整合 | UnifiedRetriever | `rag/retrieval.py` |
| 17. 工具路由 | ToolRouter | `tool_router.py` |
| 22. Search Request | SearchRequest | `search/types.py` |
| 23. Query Planner | QueryBuilder.plan() | `search/query.py` |
| 24. Provider fallback | ProviderManager | `search/provider_manager.py` |
| 25. 結果標準化 | SearchResult | `search/types.py` |
| 26. 初步過濾 | PreFilter | `search/prefilter.py` |
| 27. Web Fetch 執行 | WebFetcher.fetch_many() | `fetch/fetcher.py` |
| 28. Fetch 狀態 | FetchStatus enum | `search/types.py` |
| 29-30. Parser + Cleaner | HTMLParser + ContentCleaner | `parser/` |
| 31. Chunk | Chunker | `chunking.py` |
| 32. Dedup | Deduplicator | `dedup.py` |
| 33. Reranker | Reranker | `reranker.py` |
| 34. 證據選取 | EvidenceSelector | `evidence_selector.py` |
| 35. Context Builder | ContextBuilder | `rag/context_builder.py` |
| 37. 引用映射 | CitationResolver | `citation.py` |
| 38. 回傳格式 | SearchResponse | `search/types.py` |
| 39. Search Status | SearchStatus enum | `search/types.py` |
| 40. 回傳使用者流程 | ResponseValidator | `response_validator.py` |
| 42. Search Trace | SearchTrace | `trace.py` |
| 43. Cache 命中 | WebCache.get_or_put() | `search/cache.py` |
| 44. 中斷與降級 | Pipeline 降級策略 | `pipeline.py` |

## 來源分類

Context Builder 至少區分四種來源，不混淆：

| Source Type | 說明 |
|------------|------|
| `LOCAL_RAG` | 本機向量資料庫 / 文件檢索 |
| `SQL` | 本機 SQL 資料庫查詢 |
| `WEB_SEARCH` | 網路搜尋結果 |
| `MODEL_KNOWLEDGE` | 模型自身知識 |

## 安全限制

URLSafetyChecker 預設禁止：
- `file://`, `ftp://`, `gopher://`, `dict://` 等危險 scheme
- `localhost` / `127.0.0.1` 任意探測（除非明確授權的本地服務如 SearXNG）
- 私有網段：`10.x.x.x`, `172.16-31.x.x`, `192.168.x.x`, `169.254.x.x`
- 雲端 metadata 端點 `169.254.169.254`
- DNS rebinding 到內網

ResponseValidator 偵測並移除：
- Prompt Injection（"ignore previous instructions" 等）
- 系統指令標記（`<|system|>`, `<|im_start|>system`）
- 不存在的來源引用
- 時間衝突

## 快取 TTL 分級

| CacheTier | TTL | 適用 |
|-----------|-----|------|
| `REALTIME` | 60s | 即時資訊、價格、行情 |
| `NEWS` | 15min | 新聞 |
| `SEARCH_RESULT` | 5min | 搜尋結果 |
| `DOCUMENTATION` | 2h | 文件 |
| `STATIC` | 24h | 靜態知識頁 |

## 降級策略（需求 44）

```
Reranker 失敗       → 使用基本相關度排序
部分 Fetch 失敗     → 使用其他成功來源
SearXNG 失敗        → 使用 Fallback Provider
全部 Web Search 失敗 → 回到 Local RAG / SQL / Model Knowledge
                     → 明確表示無法確認最新網路資訊
```

不得因單一工具失效造成整個星澄無法回答。

## 快速開始

```python
from xingcheng_tools import WebSearchPipeline, PipelineConfig

# 1. 建立 pipeline（預設使用本機 SearXNG http://127.0.0.1:8080）
pipeline = WebSearchPipeline(PipelineConfig(
    searxng_url="http://127.0.0.1:8080",
    max_results=10,
    token_budget=2048,
))

# 2. 執行搜尋管線
result = pipeline.search("PyTorch 最新版本是什麼？")

# 3. 檢查結果
print(f"Status: {result.response.status.value}")
print(f"Evidence count: {len(result.evidence)}")
print(f"Context tokens: {result.context.token_estimate}")

# 4. 模型回答後的後處理（Citation + Validation）
final = pipeline.generate_answer(
    result,
    model_answer="PyTorch 最新版本是 2.13.0 [source:abc123]...",
    user_question="PyTorch 最新版本是什麼？",
)
print(final["answer"])
print(final["validation"])
```

### 意圖判斷

```python
from xingcheng_tools import IntentRouter

router = IntentRouter()
result = router.classify("今天最新新聞是什麼？")
print(result.needs_web_search)  # True

result = router.classify("什麼是 Transformer 的原理？")
print(result.needs_web_search)  # False
```

### 工具路由

```python
from xingcheng_tools import ToolRouter

router = ToolRouter()
route = router.classify("我的投資組合有哪些持股？")
print(route.primary_tool)       # ToolType.RAG
print(route.secondary_tools)    # [ToolType.SQL, ToolType.LOCAL_LLM]
```

### URL 安全驗證

```python
from xingcheng_tools import URLSafetyChecker, URLSafetyError

checker = URLSafetyChecker()
try:
    checker.validate("file:///etc/passwd")      # → URLSafetyError
    checker.validate("http://192.168.1.1/")    # → URLSafetyError
    checker.validate("https://example.com/")   # → OK
except URLSafetyError as e:
    print(f"blocked: {e}")
```

## 執行測試

```powershell
cd E:\GPTBridge\local-model\src\backend\services\xingcheng\infrastructure
python xingcheng_tools\tests\test_tools.py -v
```

41 項測試全數通過，涵蓋：Source Metadata、Intent Router、Tool Router、
Query Builder、URL Safety（7 項）、Web Cache、Pre-Filter、HTML Parser、
Content Cleaner、Chunker、Deduplicator、Reranker、Evidence Selector、
Context Builder、Citation Resolver、Response Validator、Search Trace、
Unified Retriever、Pipeline（no-search 路徑）。

## 與模型核心的關係

```
native_transformer/（模型核心）
  ↕ 透過 Context Builder 傳遞整理過的證據
xingcheng_tools/（工具與知識取得層）
```

- 模型核心負責：理解、推理、回答
- 工具層負責：取得與整理外部資料
- Context Builder 是兩者之間的唯一介面
- 網路內容不得直接控制模型（Response Validator 防護）
- 所有模組保持解耦，允許個別替換、測試、快取、降級與效能最佳化
