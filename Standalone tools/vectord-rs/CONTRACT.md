# vectord/v1 — 受管 Rust 向量引擎契約

`vectord` 是 codex A610 指定的 target-primary 語意索引引擎
（`Rust-Vector-Engine=target-primary-semantic-index`），接管 Qdrant 的
dense ANN 檢索職責。它是 `rust-component` 層的受管 loopback 服務，
雙方只能透過本契約溝通，禁止 import 內部實作。

## 權限邊界（DATA-SAFETY，法典 A610）

- vectord 只保存**可重建的衍生資料**（向量 + 最小 metadata payload），
  絕不取代 PostgreSQL 的正式資料權威。
- scope／revision／tombstone 的正式裁決仍在 PostgreSQL：vectord 回傳
  candidate IDs，由 PostgreSQL 端完成正式驗證。
- payload 不得含內容本文或實體路徑（延續 A371 禁欄位：content、text、
  path、physical_location、windows_path、source）。
- 每筆 point 必須攜帶 `module_id` scope 欄位（A52，fail-closed）。

## 傳輸

- Loopback HTTP/JSON；只綁定 loopback（`127.0.0.1` / `localhost` /
  `::1`）。非 loopback listen 位址直接拒絕啟動。
- 預設端點：`http://127.0.0.1:8092`
- 單請求 body 上限 64 MiB。

## Endpoints

### `GET /healthz`

```json
{"ok": true, "service": "vectord", "version": "1.0.0",
 "contract": "vectord/v1", "collections": 1, "points": 0, "uptime_s": 3}
```

### `POST /v1/collections/ensure`

```json
{"name": "gptbridge_shared_knowledge", "dimension": 2560}
```

已存在且維度相符 → `{"ok": true}`；維度不符 →
`{"ok": false, "error": "INDEX_MISMATCH:..."}`（fail-closed，不覆寫）。

### `POST /v1/collections/list` · `POST /v1/collections/info` · `POST /v1/collections/delete`

list 回 `{collections: [name...]}`；info 傳 `{name}` 回
`{points_count, dimension}`；delete 傳 `{name}` 回 `{existed}`。

### `POST /v1/aliases/set` · `get` · `list` · `delete`

A486 世代切換：alias→collection 映射由引擎保存並持久化。

### `POST /v1/points/upsert`

```json
{"collection": "…", "points": [{"id": "p1", "vector": [0.1, ...],
 "payload": {"module_id": "main-system", "resource_id": "r1"}}]}
```

維度不符 → `DIMENSION_MISMATCH`；未知 collection → `COLLECTION_MISSING`。

### `POST /v1/search`

```json
{"collection": "…", "vector": […], "top_k": 10, "score_threshold": 0.0,
 "filter": {"must": [{"key": "module_id", "match": {"any": ["main-system"]}}]}}
```

回 `{hits: [{id, score, payload}]}`，score 為 cosine 相似度（1 − distance）。

### `POST /v1/points/delete` · `POST /v1/points/count`

傳 `{collection, filter}`。filter DSL：`must`（全成立）+ `should`
（任一成立）+ `must_not`；condition 為 `{key, match: {value|any}}`。

### `POST /v1/snapshot`

立即把全部 collection + alias 持久化到 `--store-dir`
（預設 `runtime/`、`VECTOR_STORE_DIR` 可覆寫）。引擎每 2 秒對 dirty
狀態自動 snapshot；重啟時載入 snapshot，缺失或損毀則以空索引啟動
（衍生資料可由 PostgreSQL 正式資料重放重建）。

## 錯誤

`{"ok": false, "error": "INVALID_JSON" | "NOT_FOUND" |
"COLLECTION_MISSING" | "INDEX_MISMATCH:..." | "DIMENSION_MISMATCH:..."}`

## 刪除語意

引擎內刪除為 tombstone（查詢與計數皆排除）；快照重建時永久移除，
與 PostgreSQL tombstone-first 契約一致。
