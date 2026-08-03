# GPTBridge 本地 SQL 現況盤點與缺口分析

**評估日期**：2026-09-04
**範圍**：GPTBridge 全專案的 SQL 資料層（PostgreSQL 治理架構、local-ai RAG、模組本地 SQLite）

---

## 一、SQL 資料層總覽

GPTBridge 的資料層分為**兩大引擎**與**三種典範**：

| 引擎 | 典範 | 用途 | 治理 | 連線方式 |
|------|------|------|------|----------|
| **PostgreSQL** | 治理中央索引 + 模組私有庫 + 星澄私有庫 | 跨模組索引、RAG 元資料、工具佇列、審計、身份/認知 | RLS + NOLOGIN 角色 + SECURITY DEFINER | psycopg/binary + psycopg_pool |
| **SQLite** | 模組本地狀態庫 | 模型註冊、命令歷史、推理記錄、訓練快照 | （檔案層級） | 內建 sqlite3 |

---

## 二、PostgreSQL 架構詳析

### 2.1 提供的資料庫與 Schema

#### A. 中央索引庫（`shared-layer/sql/central_index.sql` — 301 行）
這是**跨模組治理的單一事實來源**，schema：

| Schema | 資料表 | 用途 |
|--------|--------|------|
| `gptbridge_index` | `resource` | 跨模組資源索引（含分類、版本、locator、metadata） |
| `gptbridge_index` | `resource_relation` | 資源間關係（來源/目標） |
| `gptbridge_rag` | `chunk` | RAG 文本片段（含 qdrant_point_id） |
| `gptbridge_rag` | `index_state` | RAG 索引狀態 |
| `gptbridge_transport` | `tool_request` | 跨工具請求佇列（channel/request 主鍵） |
| `gptbridge_audit` | `event` | 審計事件 |
| `gptbridge_security` | `principal` / `principal_scope` | 角色與範圍授權 |

**安全機制**：
- 6 個 schema 全部 `REVOKE ALL FROM PUBLIC`
- 所有業務表 `ENABLE + FORCE ROW LEVEL SECURITY`
- `gptbridge_security.can_read / can_write / can_write_resource` 為 `SECURITY DEFINER` 函數，依 `pg_has_role` 判斷
- NOLOGIN 群組角色：`gptbridge_index_reader / gptbridge_index_executor / gptbridge_xingcheng_reader / gptbridge_transport_executor`
- `can_write_resource` 特別限制星澄不得寫入 `permission*` / `governance-rule` 分類

#### B. 模組私有庫模板（`shared-layer/sql/module_private_template.sql` — 73 行）
每個模組擁有自己的 PostgreSQL 資料庫，套用此模板：

| Schema | 資料表 | 用途 |
|--------|--------|------|
| `module_data` | `resource` / `locator_map` | 模組私有資源 + 對映 |
| `module_state` | `operation` | 模組操作狀態 |
| `module_audit` | `event` | 模組審計 |

- 每模組建立 `gptbridge_module_<id>_reader` 與 `_executor` 兩個群組角色
- `provision_postgresql_architecture.py` 依 module_id 建立 RLS policy（按 `module_id` 欄位隔離）
- 部署時需以 MODULE_ROLE / MODULE_ID 取代模板佔位

#### C. 星澄認知庫（`local-model/local-ai/databases/cognition.sql` — 85 行）

| Schema | 資料表 | 用途 |
|--------|--------|------|
| `cognition` | `model_data` | 模型資料（jsonb） |
| `cognition` | `knowledge` | 知識條目 |
| `cognition` | `model_capability` | 模型能力設定 |
| `cognition` | `rag_reference` | RAG 交叉引用 |

- 全表 RLS + `gptbridge_xingcheng_internal` 角色
- 有 `UNIQUE (resource_id)`、`resource_label UNIQUE`、`version > 0` 約束
- 有 `content_hash` 欄位但**未設唯一/不可變約束**（對照 SQLite 快照的 `SNAPSHOT_IMMUTABLE`）

#### D. 星澄身份庫（`local-model/local-ai/databases/identity/*.sql` — 4 個模組）

| 模組 | Schema | 資料表 | 用途 |
|------|--------|--------|------|
| 001_role_data | `role_data` | `personality` | **單一人格設定**（`role_data_single_personality` 唯一索引，強制最多 1 筆） |
| 002_role_history | `role_history` | `personality_version` | 人格版本歷史（`resource_id,version` 主鍵） |
| 003_role_audit | `role_audit` | `event` | 人格變更審計 |
| 004_access_policy | — | — | RLS + role | `gptbridge_xingcheng_internal` |

- `personality` 有 `CHECK` 強制 `module_id='local-ai'`、`owner_id='local-ai'`
- 由 `provision_postgresql_architecture.py` 的 `_execute_sql_modules` 依序套用

---

## 三、連接與使用現況

### 3.1 環境變數（start.ps1 載入）
| 變數 | 用途 |
|------|------|
| `GPTBRIDGE_POSTGRES_DSN` | 中央索引庫（local-ai RAG 用之） |
| `GPTBRIDGE_POSTGRES_ADMIN_DSN` | 治理架構佈署（provision 腳本） |
| `GPTBRIDGE_MODULE_DSNS` | 模組私有資料庫 DSN 對映 |
| `GPTBRIDGE_XINGCHENG_IDENTITY_DSN` | 星澄身份庫 |
| `GPTBRIDGE_XINGCHENG_COGNITION_DSN` | 星澄認知庫 |

### 3.2 Python 存取層現況
| 存取層 | 檔案 | 使用的庫 | 覆蓋範圍 |
|--------|------|----------|----------|
| `SharedLayerStore` | `shared-layer/src/shared_layer/store.py` | psycopg + psycopg_pool（**連線池**） | `gptbridge_transport.tool_request` |
| `PostgresRagRepository` | `local-model/.../infrastructure/postgres_rag_repository.py` | psycopg + psycopg_pool（連線池） | `gptbridge_index.*`、`gptbridge_rag.*` |
| 本地 SQLite `repository.py` | `local-model/.../infrastructure/repository.py` | 內建 sqlite3 | `local-ai/runtime/state/models/*.sqlite3` |
| 本地 SQLite `local_command_parser.py` | `local-ai/application/local_command_parser.py` | 內建 sqlite3 | 常用命令歷史 |
| 本地 SQLite `ollama_model_repository.py` | `local-model/.../infrastructure/ollama_model_repository.py` | 內建 sqlite3 | 推論記錄、能力投票、訓練貢獻 |

---

## 四、關鍵缺口分析

### 缺口 1（最高）：星澄認知/身份庫「只有 Schema、沒有 Python 存取層」
- `cognition.sql`（model_data / knowledge / model_capability / rag_reference）與
  `identity/*.sql`（personality / personality_version / event）**已由 provisioning 腳本建立**，
  但專案內**沒有任何 Python Repository 讀寫它們**。
- 唯一的引用是測試檔 `test_local_ai_layering.py` 的靜態字串檢查。
- 結果：這些治理孤立的資料表形同虛設；星座的人格設定、版本歷史、審計無法被 local-ai 使用。

### 缺口 2：星澄認知庫缺不可變/合規約束
- `cognition.sql` 的 `content_hash`、`version` 未套用 SQLite 快照那樣的
  `SNAPSHOT_IMMUTABLE` / `AUDIT_IMMUTABLE` 約束（對照 `test_transformer_training_repository.py`）。
- 既有審計表 `gptbridge_audit.event` 也未見「不可變」觸發器。

### 缺口 3：工具佇列缺少佇列擷取通知的服務化（部分改善）
- `store.py` 已改用連線池；`submit_request` 已加 `pg_notify`。
- 但**通知監聽**在 `governed_runtime.py` 用獨立 psycopg 連線 `LISTEN`，與資料庫角色的 RLS 權限互動未充分驗證。

### 缺口 4：模組私有庫/身份庫無開發期驗證路徑
- `module_private_template.sql` 與 `identity/*.sql` 僅由 provision 腳本執行，缺**配對的回歸測試**確認
  DSN、角色、RLS policy 正確建立（現有測試只檢查 `cognition.model_data` 存在於字串）。

### 缺口 5：連線池型態不一致
- `SharedLayerStore` 與 `PostgresRagRepository` 各自建立自己的 `ConnectionPool`，未共用；
  星澄認知/身份若新增存取層，需沿用同一連線池策略而非重複開連線。

---

## 五、建議補全方向（優先序）

| 優先序 | 項目 | 說明 | 風險 |
|--------|------|------|------|
| 1 | 新增 `PostgresCognitionRepository` | 實作 `cognition.model_data/knowledge/model_capability/rag_reference` 的受治理 CRUD（繼承現有 RLS 角色設計） | 中 |
| 2 | 新增 `PostgresIdentityRepository` | 實作 `personality` 單筆 + 版本歷史 + 審計，沿用 `role_data_single_personality` 約束 | 中 |
| 3 | 統一連線池 | 抽取共用 PostgreSQL 連線池管理，避免各 Repository 各自開池 | 低 |
| 4 | 補不可變約束 | 為星澄認知/審計表加內容 hash 防篡改觸發器 | 低 |
| 5 | 補 DSN/架構回歸測試 | 驗證 cognition/identity/module_private 佈署結果 | 低 |
| 6 | 服務化佇列通知 | 將 LISTEN/NOTIFY 從獨立連線提升為治理可驗證的服務 | 中 |

> 註：SQLite 本地庫（模型註冊、命令歷史、推論記錄）功能完整、有測試覆蓋，**非當前補全重點**。

---

## 六、結論

PostgreSQL 治理架構的 **Schema 設計完整且嚴謹**（RLS + NOLOGIN 角色 + SECURITY DEFINER + REVOKE PUBLIC），
但 **「星澄認知/身份」兩組資料庫只有 Schema 沒有 Python 存取層**，是當前「本地 SQL 整合」最明確的缺口。
補全應以**新增認知/身份 Repository + 統一連線池**為起點，配合不可變約束與回歸測試，即可讓既有的治理 SQL 架構真正投入運作。
