# xingcheng-searchd/v1 — 受管 metasearch 服務契約

`searchd` 是 go-service 層的 bounded concurrent service。它只透過本
契約對外溝通；雙方皆不得 import 對方內部實作（DependencyPolicy：
`go-service → channel-api`）。

## 傳輸

- Loopback HTTP/JSON。服務只綁定 loopback（`127.0.0.1` / `localhost` /
  `::1`）；非 loopback listen 位址啟動即拒絕。
- 預設端點：`http://127.0.0.1:8091`

## Endpoints

### `GET /healthz`

```json
{"ok": true, "service": "searchd", "version": "1.0.0", "contract": "xingcheng-searchd/v1"}
```

### `POST /v1/search`

Request body（≤64 KiB）：

```json
{
  "request_id": "optional-caller-id",
  "query": "查詢字串（≤400 字元）",
  "language": "zh-TW",
  "max_results": 5,
  "time_range": "",
  "sites": ["wikipedia.org"],
  "safesearch": true
}
```

Response：

```json
{
  "ok": true,
  "contract": "xingcheng-searchd/v1",
  "engine": "searchd",
  "request_id": "...",
  "query": "...",
  "results": [
    {
      "title": "...", "url": "https://...", "domain": "...",
      "snippet": "...", "published": "...", "engine": "wikipedia",
      "rank": 0, "score": 0.032
    }
  ],
  "adapters": [
    {"name": "wikipedia", "ok": true, "count": 3, "latency_ms": 120},
    {"name": "bing", "ok": false, "count": 0, "latency_ms": 8000,
     "error": "context deadline exceeded"}
  ]
}
```

錯誤：`{"ok": false, "error": "QUERY_REQUIRED" | "INVALID_JSON" |
"METHOD_NOT_ALLOWED"}`。

## 語意保證

- `results` 僅含 metadata（title/url/domain/snippet/published），絕不含
  完整頁面內容；服務端從不抓 result URL。
- snippet ≤300 字元、title ≤200 字元，HTML 標籤與控制字元已清除。
- 跨 adapter 去重（URL 正規化：lowercase host、去 `www.`、去
  `utm_*`/`fbclid`/`gclid`、去 fragment、去尾斜線）。
- 排序為 deterministic reciprocal-rank fusion（k=60）：跨來源重複命中
  的結果分數較高；同分按 URL 字典序。
- 每個 adapter 狀態暴露在 `adapters[]` — 上游失敗降為 partial
  success，不會讓整個請求失敗。
- `max_results` 上限 20；`sites` 非空時僅保留該網域（含子網域）結果。

## 安全邊界（fail-closed）

- 出站目的地為**編譯期固定 allowlist**：
  `*.wikipedia.org`、`lite/html/api.duckduckgo.com`、`www.bing.com`。
  由 transport RoundTripper 強制 — 任何非 allowlist host（含 redirect
  目標）被拒絕。使用者輸入無法新增目的地，服務不可用作任意 proxy。
- 上游回應 body 上限 1 MiB；每 adapter timeout 8s；全域 15s。
- 上游 URL 只允許 http/https 且 host 非空，否則結果被丟棄。
