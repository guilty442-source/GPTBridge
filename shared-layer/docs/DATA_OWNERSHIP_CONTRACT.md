# Data Ownership Contract

> **法典依據：** A8（系統責任）、A44/E30（四功能本機）、A49/E35（正式工具）、A52/E38（RAG 四子架構）

## 1. 三層資料所有權

```
┌─────────────────────────────────────────────────────────────────┐
│                        資料所有權架構                             │
├──────────────────┬──────────────────┬──────────────────────────┤
│   PostgreSQL     │     Qdrant       │   SQLite / NTFS          │
│   中央索引        │     向量索引      │   原始 / 模組私有         │
├──────────────────┼──────────────────┼──────────────────────────┤
│ 中央索引與關聯真相 │ 可重建的向量索引  │ 原始或模組私有資料         │
│                  │                  │                          │
│ • resource 元資料 │ • 向量 + payload │ • 原始檔案內容（NTFS）     │
│ • locator 對應    │ • 可從 SQLite    │ • 模組私有業務資料(SQLite) │
│ • 跨模組關聯      │   + 嵌入模型重建  │ • 本機執行期狀態           │
│ • 審計事件        │ • 永遠不是唯一    │ • 訓練檢查點               │
│ • transport 請求  │   資料的唯一存放  │ • 模組私有設定             │
│ • 版本表          │   位置           │                          │
├──────────────────┼──────────────────┼──────────────────────────┤
│ 真相來源          │ 快取層           │ 原始層                    │
│ (Source of Truth) │ (Cache)          │ (Origin)                  │
└──────────────────┴──────────────────┴──────────────────────────┘
```

## 2. 不可破壞的規則

| 規則 | 說明 |
|------|------|
| **Qdrant 永遠可以重建** | 不可重建的唯一資料絕不能只放在 Qdrant。Qdrant 的向量必須可從 SQLite 原始內容 + 嵌入模型重新生成。 |
| **PostgreSQL 是中央真相** | 當 PostgreSQL 與 SQLite 衝突時，以 PostgreSQL 版本為準（除非 SQLite 有較新版本且 PostgreSQL 曾故障）。 |
| **SQLite/NTFS 是原始層** | 原始檔案內容、模組私有業務資料存於此。PostgreSQL 中央索引只存元資料 + locator，不存原始內容。 |
| **不雙向同步** | Reconcile 是單向的：SQLite → PostgreSQL（故障恢復後）。不是雙向亂同步。 |
| **實體路徑不公開** | locator_id 是 opaque 的。實體路徑只在 registry.locations 中，透過安全視圖存取。 |

## 3. 故障降級與恢復流程

```
正常運作：
  SQLite (原始) → PostgreSQL (中央索引) → Qdrant (向量索引)

PostgreSQL 故障：
  SQLite (原始) ──繼續運作──→ 標記變更為 pending reconcile
  Qdrant 繼續服務（使用既有向量）

PostgreSQL 恢復：
  ReconcileService.reconcile_module(module_id)
    ├─ local_version > central → push to central
    ├─ local_version < central → pull from central (central wins)
    ├─ same version, hash differs → conflict (flag for review)
    └─ same version, same hash → in-sync (no-op)

Qdrant 故障：
  從 SQLite 原始內容 + 嵌入模型重建向量索引
  PostgreSQL 中央索引的 qdrant_point_id 欄位保留映射

SQLite 故障：
  模組私有資料遺失（如果無 NTFS 備份）
  PostgreSQL 中央索引仍保有元資料 + locator
```

## 4. Metadata Contract（固定欄位）

所有模組、所有儲存層必須使用以下固定欄位：

| 欄位 | 類型 | 說明 |
|------|------|------|
| `module_id` | text | 擁有模組（kebab-case） |
| `resource_id` | text | 模組範圍資源鍵（kebab-case） |
| `locator_id` | uuid | opaque locator（`locator_id_for(module_id, resource_id)`） |
| `version` | int | 單調遞增版本（≥ 1） |
| `content_hash` | text | SHA-256 hex digest，或 NULL |
| `updated_at` | timestamptz | 最後變更時間 |
| `status` | text | 生命週期狀態 |

Python 模組：`shared_layer.metadata_contract.ResourceMetadata`

## 5. Qdrant Payload 契約

每個 Qdrant point 必須攜帶：

| 欄位 | 必填 | 說明 |
|------|------|------|
| `module_id` | ✅ | 模組隔離 |
| `resource_id` | ✅ | 資源關聯 |
| `chunk_id` | ✅ | 分塊識別 |
| `version` | ✅ | 版本綁定 |
| `locator_id` | ✅ | 中央索引關聯 |

驗證：`shared_layer.metadata_contract.validate_qdrant_payload()`

## 6. PostgreSQL Role 隔離

| 角色 | 權限 | 說明 |
|------|------|------|
| `gptbridge_owner` | 全部 | 部署管理員（NOLOGIN） |
| `gptbridge_index_reader` | SELECT | 全域索引唯讀 |
| `gptbridge_index_executor` | SELECT/INSERT/UPDATE | 索引 + RAG + transport + audit |
| `gptbridge_xingcheng_reader` | SELECT (opaque view) | 星澄唯讀（不含實體路徑） |
| `gptbridge_transport_executor` | transport + audit | 跨工具請求執行 |
| `gptbridge_module_{id}` | SET ROLE | 每個模組一個 NOLOGIN 角色 |
| `gptbridge_xingcheng_internal` | cognition/role | 星澄內部資料庫 |

**RLS + Role 雙重隔離：** 即使 RLS 策略意外寬鬆，GRANT/REVOKE 仍在表權限層隔離模組。

## 7. Audit Append-Only

`gptbridge_audit.event`：
- 所有 executor 角色只有 `SELECT + INSERT` 權限
- `UPDATE` 和 `DELETE` 被 REVOKE
- RLS 無 UPDATE/DELETE policy → 即使有權限也被 RLS 阻擋
- 觸發器 `prevent_audit_mutation()` → 即使超級用戶也被阻擋（最後防線）

## 8. Transport Idempotency

`gptbridge_transport.tool_request`：
- `idempotency_key` 欄位 + `(target_tool_id, idempotency_key)` 唯一索引
- 重試相同請求時，使用相同 idempotency_key → 返回原始回應，不重複執行
- `processed_at` + `response_payload` 記錄已處理結果

## 9. 版本表

| 表 | 位置 | 職責 |
|----|------|------|
| `gptbridge_index.schema_version` | PostgreSQL | 全域 schema 遷移版本 |
| `gptbridge_index.module_version` | PostgreSQL | 每個模組的本地版本（含 SQLite） |
| `schema_version`（SQLite 模板） | SQLite | 每個 SQLite DB 的本地 schema 版本 |

**避免升級不同步：** PostgreSQL 的 `module_version` 表記錄每個模組聲明的 SQLite 版本。Reconcile 時可比對版本，偵測落後或超前。
