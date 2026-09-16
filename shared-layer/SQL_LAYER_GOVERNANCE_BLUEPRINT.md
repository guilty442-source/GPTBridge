# SQL 層治理藍圖（Data Lineage → Recovery Certification）

本藍圖把「資料血緣 → 寫入權威 → 一致性證明 → 故障恢復證明」20 條規格逐條
對應到既有資產，標出缺口，並排定分階段、分模組的實作計畫。**本檔只規劃，不
含程式碼變更。**

> 既有資產命名慣例：
> - PostgreSQL schema/遷移：`shared-layer/migrations/NNN_<slug>.sql`
> - 中央 schema 定義：`shared-layer/sql/central_index.sql`
> - SQLite 模組樣板：`shared-layer/sql/sqlite_module_template.sql`
> - runtime 治理層：`main-system/src-core/core_system/`
> - 共享資料庫契約/查詢：`shared-layer/src/shared_layer/database/`
> - 法典：`governance_rule/codex/`（**唯讀，改動須使用者核准**）

---

## 0. 規格 → 既有資產對應總表

| # | 規格項 | 既有覆蓋 | 缺口 | 落點 |
|---|--------|----------|------|------|
| 1 | Data Lineage（PG→Qdrant→SQLite 反查鏈） | 部分：`resource` 有 `module_id/owner_id/version/content_hash`；`index_state` 有 `source_revision`；`registry.locations` 有 locator | 缺獨立 `data_lineage` 表記錄 source_module/source_revision/produce_method/sync_path/last_writer；缺跨引擎反查 view | migration 018 + view |
| 2 | Authority Marker（`authority_class`） | 無顯式欄位 | `resource` / `index_state` / SQLite template 缺 `authority_class` 欄與 CHECK 限制 | migration 019 + template |
| 3 | Write Provenance（actor/executor/decision/correlation/source_revision） | `audit.event` 有 `actor_id/decision_id`；`tool_request` 有 `requester_actor` | `resource`/`index_state` 寫入列缺 `executor_id/correlation_id/source_revision`；缺統一 provenance 欄位 | migration 020 |
| 4 | 權限證明快照 | `audit.event.details` 可放，但無結構化 schema | 缺 `permission_snapshot` 結構（evaluated_rules/decision_summary/rls_context）寫入 audit | migration 021 + audit writer |
| 5 | Schema Ownership 鎖定 | 有角色分離 + REVOKE；`schema_contract_registry` 宣告契約 | 缺 `ALTER SCHEMA ... OWNER TO` 鎖定單一 owner；缺禁止 runtime role DDL 的 trigger/event | migration 022 |
| 6 | DDL Audit | 無獨立 DDL audit | 缺 `ddl_audit.event` 表 + event trigger 記錄 schema/index/RLS/role 變更 | migration 023 |
| 7 | 資料契約版本握手 | `schema_contract_registry` 宣告契約；`004` 有 version table | 缺連線時 `contract_version` 宣告與不相容即拒寫的 fence | migration 024 + 連線層 |
| 8 | Connection Generation Fence | **已實作**：`016` 的 `backend_generation_state` + `enforce_generation_fence` trigger | 缺 SQLite/Qdrant 端 generation 同步；缺 restore/migration/role-rotation 自動 bump 的 runtime hook | migration 025（補 SQLite/template）+ runtime |
| 9 | Long Transaction Watchdog | 無 | 缺 `pg_stat_activity`/`idle-in-transaction`/lock holder 監控 + 告警/中止 | runtime watcher |
| 10 | Bloat / Vacuum 控制 | `017` maintenance_window 表；無 bloat 追蹤 | 缺 `pg_stat_user_tables` bloat/dead tuple/autovacuum lag 追蹤；transport/audit 無獨立策略 | runtime collector + 015 擴充 |
| 11 | Workload Class | `pool_isolation` 有 4 個 owner（central/transport/audit/module） | 缺 `interactive/transport/audit/reconciliation/maintenance/migration` 六類分級 + 各自 timeout/pool/priority | migration 026 + pool 擴充 |
| 12 | 資料刪除兩階段化 | `009` 有 `tombstone_generation`；`015` retention_policy | 缺 `tombstone → retention window → purge` 狀態機；缺跨 PG index/SQLite/Qdrant 同步刪除 | migration 027 + runtime |
| 13 | Orphan Scanner | `012` `resource_consistency` view 找 missing-qdrant/behind | 缺獨立 orphan scanner：PG 有 resource 但 locator 不存在、Qdrant point 無 chunk、SQLite pending 但 PG 無對應 | runtime scanner |
| 14 | Rebuild Certification | `016` backup_catalog 有 `restore_certified`/`restore_certification` | 缺 count/hash/revision/RLS/locator 驗證流程的 runtime 實作 | runtime certifier |
| 15 | SLO | `shared-layer/observability/` 有 tracing/endpoint | 缺 SQL 層正式 SLO 指標（p95/transport claim latency/reconcile backlog/SQLite lock rate/Qdrant stale rate/restore success） | observability 擴充 |
| 16 | RPO / RTO 分級 | `016` backup_catalog；無分級 | 缺中央 PG/治理 SQLite/模組 SQLite/Qdrant 各自 RPO/RTO 宣告與備份排程 | migration 028 + runtime |
| 17 | 容量水位線 | 無 | 缺 `warning/critical/fail-closed` 三段水位（磁碟/WAL/SQLite WAL/transport backlog/Qdrant collection size） | runtime monitor |
| 18 | 只讀緊急模式 | 無 | 缺指定資料域切 read-only（integrity/schema drift/authority conflict）而非整系統停機 | migration 029 + runtime |
| 19 | 資料庫啟動認證 | `governed_startup` 有 phase/DAG；`schema_contract_registry` 比對 drift | 缺啟動時驗證 schema version/RLS/required roles/migration head/audit append-only/authority contract 後才標 READY 的 SQL 層專屬 gate | runtime gate |
| 20 | （總結四能力） | — | — | 驗收 |

---

## 1. 分階段實作計畫

### Phase A — 資料能說出從哪來（Lineage + Authority + Provenance）

**目標**：每筆中央資源可反查整條 `SQLite → PostgreSQL → Qdrant` 鏈，並標明權威等級與寫入來源。

| 工作包 | 內容 | 落點 | 依賴 |
|--------|------|------|------|
| A1 | `018_data_lineage.sql`：新增 `gptbridge_index.data_lineage` 表（`resource_id, source_module, source_revision, produce_method, sync_path, last_writer_id, last_writer_at`）+ 觸發器在 `resource` INSERT/UPDATE 時自動補寫 lineage | `shared-layer/migrations/` | — |
| A2 | `gptbridge_index.resource_lineage` view：join `resource` + `data_lineage` + `registry.locations` + `index_state`，一次查完整鏈 | 同 018 | A1 |
| A3 | `019_authority_marker.sql`：`resource`/`index_state` 加 `authority_class text NOT NULL DEFAULT 'derived' CHECK (authority_class IN ('central-official','module-private','derived','cache','degraded-copy'))`；SQLite template 同步加欄 | `shared-layer/migrations/` + `sql/sqlite_module_template.sql` | — |
| A4 | `020_write_provenance.sql`：`resource`/`index_state` 加 `executor_id, correlation_id, source_revision`；`audit.event` 加 `executor_id, correlation_id, source_revision`；用 `current_setting('gptbridge.*', true)` 在 trigger 內自動填入 | `shared-layer/migrations/` | A3 |
| A5 | runtime：`shared_layer.database` 寫入輔助 `set_provenance(conn, actor_id, executor_id, decision_id, correlation_id, source_revision, generation)` — 統一 `SET LOCAL` 所有 session 變數 | `shared-layer/src/shared_layer/database/` | A4 |
| A6 | `query_allowlist` 擴充：所有 insert/update template 帶 provenance 欄 | `shared-layer/src/shared_layer/database/query_allowlist.py` | A4 |
| A7 | ~~法典修訂~~ — **由使用者負責**，不列入本藍圖實作範圍 | `governance_rule/codex/` | A1–A4 |

**驗收**：任一 `resource_id` 可經 `resource_lineage` view 一次查出 source module、revision、produce method、sync path、last writer、authority class、跨引擎一致性狀態。

---

### Phase B — 知道誰有權改（Permission Snapshot + Schema Lock + DDL Audit + Contract Handshake）

**目標**：寫入時留下權限決策快照；schema 結構變更只能走 migration executor 並被獨立稽核；連線先握手契約版本。

| 工作包 | 內容 | 落點 | 依賴 |
|--------|------|------|------|
| B1 | `021_permission_snapshot.sql`：`gptbridge_audit.event` 加 `permission_snapshot jsonb`（或獨立 `permission_snapshot` 表 `event_id, evaluated_rules, decision_summary, rls_context, actor_roles`） | `shared-layer/migrations/` | A4 |
| B2 | runtime：audit writer 在重要寫入前呼叫 `capture_permission_snapshot(conn)` — 讀 `pg_has_role`/RLS policy 評估結果寫入 snapshot | `main-system/src-core/core_system/` | B1 |
| B3 | `022_schema_ownership_lock.sql`：每個 schema `ALTER SCHEMA ... OWNER TO gptbridge_migration_owner`；建立 `gptbridge_migration_owner` NOLOGIN role；建立 event trigger `ddl_guard` 拒絕非 migration_owner 的 CREATE/ALTER/DROP | `shared-layer/migrations/` | — |
| B4 | `023_ddl_audit.sql`：`gptbridge_audit.ddl_event` 表 + `EVENT TRIGGER` on `ddl_command_end` 記錄 schema/index/RLS/role 變更（command_tag, object_identity, role, sql) | `shared-layer/migrations/` | B3 |
| B5 | `024_contract_version_handshake.sql`：`gptbridge_index.contract_version` 表（`contract_name, supported_version, min_compatible`）；連線 `SET LOCAL gptbridge.contract_version`；trigger 在寫入時檢查宣告版本 ≥ `min_compatible`，否則 `RAISE` 拒寫 | `shared-layer/migrations/` | A4 |
| B6 | runtime：連線池取得連線後先 `SET gptbridge.contract_version`；`schema_contract_registry` 比對 handshake 結果 | `shared-layer/src/shared_layer/database/` + `main-system/.../pool_isolation/` | B5 |
| B7 | `audit_checks.py` 新增 `check_schema_ownership`、`check_ddl_audit`、`check_contract_handshake` | `governance_rule/execution/audit/` | B3–B5 |

**驗收**：任何 DDL 在 `ddl_event` 留下記錄；runtime role 嘗試建表會被 event trigger 拒絕；宣告不相容 contract_version 的連線無法寫入。

---

### Phase C — 能證明現在一致（Generation Fence 補全 + Workload Class + 兩階段刪除 + Orphan Scanner）

**目標**：跨引擎版本對齊有 fence；SQL 流量分級不互搶；刪除走兩階段不留孤兒。

| 工作包 | 內容 | 落點 | 依賴 |
|--------|------|------|------|
| C1 | `025_sqlite_generation_fence.sql`（template 更新）：SQLite module template 加 `backend_generation` 欄 + 連線時讀 PG `current_backend_generation()` 比對的 runtime 邏輯 | `shared-layer/sql/sqlite_module_template.sql` + runtime | 016 |
| C2 | runtime：`generation_fence` 連線輔助 — 取連線即 `SET LOCAL gptbridge.connection_generation`；restore/migration/role-rotation 後自動呼叫 `bump_backend_generation` | `main-system/src-core/core_system/` | 016, C1 |
| C3 | `026_workload_class.sql`：`gptbridge_index.workload_class` 表（`class, pool_owner, statement_timeout_ms, lock_timeout_ms, priority`）六類：interactive/transport/audit/reconciliation/maintenance/migration；`SET LOCAL statement_timeout`/`lock_timeout` 由 trigger 依 `gptbridge.workload_class` 套用 | `shared-layer/migrations/` | pool_isolation |
| C4 | runtime：`pool_isolation` 擴充 `acquire_for_workload(class)` — 依 class 取池並 `SET LOCAL` 對應 timeout | `main-system/.../pool_isolation/` | C3 |
| C5 | `027_two_stage_deletion.sql`：`resource`/`index_state` 加 `deletion_stage`（`active → tombstone → retention_window → purged`）+ `tombstoned_at`/`purge_after`；trigger 在 `tombstone` 時同步標記 SQLite pending-delete 與 Qdrant point tombstone | `shared-layer/migrations/` + runtime | 009, 015 |
| C6 | runtime：`deletion_coordinator` — tombstone → 等 retention window → purge，跨 PG index/SQLite/Qdrant 同步刪除，失敗寫 reconcile queue | `main-system/src-core/core_system/` | C5 |
| C7 | runtime：`orphan_scanner` 週期任務 — 掃 `resource_consistency` view + 三類孤兒（PG resource 無 locator / Qdrant point 無 chunk / SQLite pending 無 PG 對應），結果寫 `gptbridge_index.orphan_report` | `main-system/src-core/core_system/` | 012, C5 |

**驗收**：跨引擎 stale 連線被 fence 拒絕；transport 與 maintenance 流量分池不互搶；刪除資源在 retention window 內可還原，purge 後三引擎同步無孤兒。

---

### Phase D — 能證明故障後恢復正確（Rebuild Cert + Watchdog + Bloat + SLO + RPO/RTO + 水位 + 只讀模式 + 啟動認證）

**目標**：故障後恢復須經驗證才上線；常態監控交易/bloat/容量/SLO；分級備份；可局部只讀；啟動須通過完整認證。

| 工作包 | 內容 | 落點 | 依賴 |
|--------|------|------|------|
| D1 | runtime：`rebuild_certifier` — Qdrant 重建/PG restore/SQLite repair 後跑 count/hash/revision/RLS/locator 驗證，通過才寫 `backup_catalog.restore_certified=true` + `restore_certification` | `main-system/src-core/core_system/` | 016 |
| D2 | runtime：`long_transaction_watchdog` — 週期查 `pg_stat_activity`（`state='idle in transaction'`、`xact_start` age、`wait_event_type='Lock'`），超門檻告警/`pg_terminate_backend` | `main-system/src-core/core_system/` | — |
| D3 | runtime：`bloat_collector` — 週期查 `pg_stat_user_tables`（dead tuple/last_autovacuum/last_analyze）+ 估算 bloat；transport/audit 表獨立策略寫 `maintenance_window` | `main-system/src-core/core_system/` | 017 |
| D4 | `028_rpo_rto_tier.sql`：`gptbridge_index.recovery_tier` 表（`engine, rpo_minutes, rto_minutes, backup_schedule, tier`）四引擎各自宣告 | `shared-layer/migrations/` | 016 |
| D5 | runtime：`backup_scheduler` — 依 recovery_tier 排程各引擎備份，寫 `backup_catalog` | `main-system/src-core/core_system/` | D4, 016 |
| D6 | runtime：`capacity_monitor` — 磁碟/WAL/SQLite WAL/transport backlog/Qdrant collection size 三段水位（warning/critical/fail-closed），critical 觸發告警，fail-closed 拒寫 | `main-system/src-core/core_system/` | — |
| D7 | `029_readonly_domain.sql`：`gptbridge_index.readonly_domain` 表（`domain, readonly, reason, set_at, set_by`）+ trigger 在寫入前檢查 domain 是否 readonly，是則 `RAISE` | `shared-layer/migrations/` | — |
| D8 | runtime：`readonly_governor` — integrity/schema drift/authority conflict 時切指定 domain 為 readonly 而非整系統停機 | `main-system/src-core/core_system/` | D7 |
| D9 | runtime：`sql_startup_gate` — 啟動時驗證 schema version/RLS enabled/required roles 存在/migration head/audit append-only（`006`）/authority contract（B5），全過才標 SQL 層 READY | `main-system/src-core/core_system/`（接 `governed_startup`） | B5, 006 |
| D10 | `shared-layer/observability/` 擴充：正式 SLO 指標 — 中央查詢 p95、transport claim latency、reconcile backlog、SQLite lock rate、Qdrant stale rate、restore success rate | `shared-layer/src/shared_layer/observability/` | D1–D9 |
| D11 | `audit_checks.py` 新增 `check_recovery_tier`、`check_readonly_domain`、`check_startup_gate` | `governance_rule/execution/audit/` | D4, D7, D9 |

**驗收**：restore 後未通過 certifier 的備份不會被標 certified；長交易/bloat/容量超門檻有告警；各引擎依各自 RPO/RTO 備份；發生 drift 時可切單一 domain 只讀；啟動未通過 SQL gate 則 SQL 層不 READY。

---

## 2. 跨切面向

- **法典修訂**：**由使用者負責**，不在本藍圖實作範圍。實作端會在每個 Phase 的 audit check 與 migration 註解中標明對應的規格項，供使用者後續補入法典。
- **audit 擴充**：每個 Phase 結束後在 `audit_checks.py` 補對應 check 並更新 `check_sql_migrations` 的必要遷移清單。
- **測試**：每個 migration 配 `shared-layer/tests/test_NN_*.py`；runtime 模組配 `main-system` 測試；rebuild cert/orphan scanner 配 recovery drill 測試（已有 `test_recovery_drill.py` 可擴充）。
- **worktree 分工**：藍圖本身在 main；實作時 migration + shared-layer 在 main，runtime 模組可視歸屬拆到 main-system（main）或對應 worker worktree，最後由 sync coordinator 併回 main。

## 3. 建議實作順序

A（lineage/authority/provenance 是後續所有證明的基礎）→ B（權限與結構鎖定）→ C（一致性與流量分級）→ D（恢復與監控）。Phase 內工作包大致由上而下有依賴；Phase 間 A 必須先完成，B/C 可部分重疊，D 依賴 C 的 fence 與 B 的 contract。
