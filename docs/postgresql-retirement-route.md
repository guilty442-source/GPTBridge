# PostgreSQL 退役盤點與分階段路線

> **現行文件**。取代 `sql-inventory-analysis.md`（歷史證據）的消費者面描述。
> 治理依據：使用者 2026-10-03 明確指令＋修正案
> `codex-amendment-request-native-sql-postgresql-retirement-principles-20261003-r1`
> （待五主權一致通過）。修正案核心：**project-owned in-process native SQL**
> 為唯一結構化權威目標；PostgreSQL 在驗證切換完成前僅為唯讀遷移來源，
> 不作為第二寫方；無完整 parity 證據不得宣告退役。

**盤點日期**：2026-10-03
**範圍**：全專案所有會開啟 PostgreSQL 連線或依賴 `psql`/`PG` 工具的 live 程式碼路徑

---

## 一、治理指令摘要

修正案 r1（`architecture-authority` 級，未執行）對 C16/C81/B166 等條文要求：

- 生產合約中不得有外部 SQL 執行工具、服務或子行程；
- 每領域一個註冊 canonical store；
- 切換須具備 data＋schema＋transaction＋permission＋audit＋recovery＋
  current-generation parity 證據，原子切換＋可回滾；
- 舊 PostgreSQL 資料保存為唯讀遷移來源，直到獨立驗證完成；
- 缺證據或回歸 → 保留現行已驗證擁有者或 fail-closed。

因此本文件的所有階段皆為**非破壞性、唯讀來源側**工作；
權威切換須等修正案執行且對應 parity 閘通過後才進行。

## 二、Live 消費者盤點（連線邊界）

| 消費者 | 行程 | 驅動 | PG 行為 | 狀態 |
| --- | --- | --- | --- | --- |
| CodexPipeline | `GPTBridge.CodexPipeline.exe`（經 Automation/GitAutomation/ToolHost 呼叫） | Npgsql 9.0.3 | `gptbridge_codex` 全量讀寫＋staging schema＋受管 migration 執行＋receipt | **活躍 — 法典權威** |
| GitAutomation AuditGate | `GPTBridge.GitAutomation.exe`（含 Automation git plane） | ~~`psql` 子行程~~ → `--authority-state` | `codex_authority_state.imported_at` epoch（manifest 鮮度閘） | **已改受管路徑（本文件同期）** |
| gptbridge-backend | Tauri host | `postgres` crate 0.19 | `gptbridge_transport.outbox_event` 讀＋保留修剪；`gptbridge_workflow.operation*` 唯讀診斷 | **活躍但目標表已凍結**（無 live writer） |
| ai-collab-host | Go | `pgx/v5`＋pgxpool | `gptbridge_collab`（`ai_nexus_*`）全 CRUD＋啟動 DDL | **活躍 — 工具自有資料** |
| PermissionAutomation | `GPTBridge.Automation.exe` | — | 解析 DSN 後 TCP 探測（無 SQL） | **活躍（僅 socket 探測）** |
| DSN 傳播 | Bootstrap/`automation_host.rs`/manifest.json | — | env 轉發 | 退役時移除 |

### 已退役（無 PG 依賴）

vectord-rs、ragd-rs（xstore，`--dsn` 明確拒絕）、XingchengLearning 生產路徑
（xstore metadata authority；`LegacyMigration/` 已 `Compile Remove`）、
GPTBridge.Channels（檔案傳輸取代 `pg_notify`/LISTEN-NOTIFY）、
MainSystem DependencyProbes（PG readiness 主動以 `POSTGRESQL_RETIRED` 拒絕）。

### DSN 入口

`GPTBRIDGE_POSTGRES_DSN`（PgDsn.cs、pg.rs、ai-collab dsn、AuditGate 原路徑）、
`GPTBRIDGE_POSTGRES_ADMIN_DSN`（PgDsn.cs 寫入側）、
`GPTBRIDGE_POSTGRES_READER_ROLE`（env allowlist）、
`GPTBRIDGE_MODULE_DSNS`（Bootstrap 轉發，**無任何程式碼消費者**）、
`AI_COLLAB_PG_SCHEMA`（ai-collab search_path）。
`*_XINGCHENG_*_DSN`、`*_READER_DSN`：僅存在歷史文件/證據檔——**死設定**。

## 三、Schema 分類（live catalog 觀測，約 60 schema / 394 MB）

| 類別 | Schema | 判斷 | 處置 |
| --- | --- | --- | --- |
| **權威** | `gptbridge_codex`（219 表 / 24,119 列快照） | 法典唯一權威；寫方為 CodexPipeline | Phase 最高治理等級：native snapshot 已能唯讀驗證；寫入路徑待 native SQL 引擎成熟＋修正案執行 |
| **工具自有（活）** | `gptbridge_collab`（~150 列） | ai-collab-host 唯一讀寫者 | 工具自有遷移：資料量小，適合先行試點 |
| **凍結遺留（讀者活、寫方死）** | `gptbridge_transport`（outbox_event ~1,679、tool_request ~7,335）、`gptbridge_workflow` | 寫方為已退役 Python lane；Rust 端僅讀＋修剪 | 一次性封存匯出 → 讀者改讀原生快照或連讀者一併退役 |
| **無消費者遺留** | `gptbridge_index`、`gptbridge_audit`(~25k)、`gptbridge_security`、`gptbridge_permission`、`gptbridge_maintenance`(~9.8k)、`gptbridge_repair`(~6.9k)、`gptbridge_legacy`、`gptbridge_perf`、`gptbridge_lineage`、`gptbridge_rag*`、`gptbridge_trading` 等 | 搜尋不到任何 in-repo 讀寫者（migrations/角色授權/文件提及除外） | 封存匯出保存證據 → 不建目標 schema；由治理決定保留形式 |
| **已遷移** | `gptbridge_xingcheng*` | xstore metadata authority 已接手（NativeMetadataClient；影子/parity hooks） | 驗證 parity 後凍結 PG 側 |
| **測試/暫存殘留** | `im_test_*`、`outbox_test_*`、`vect_test_*`、`gptbridge_codex_codex_stage_*` | 測試殘留 | 僅在確認無活動行程後清理（非破壞性前置：唯讀觀測） |

注意：migration registry 與 live catalog 嚴重分歧（49 條註冊鏈無 receipt、
709 declared-missing／522 live-only、89 檔未註冊、雙前綴 087/088），
故一切遷移以 **live catalog 匯出** 為準，不得以註冊鏈替代實際觀測。

## 四、已落地切片（2026-10-03）

| 切片 | 提交 | 狀態 |
| --- | --- | --- |
| xstore native SQL 讀平面（bounded SELECT＋C ABI＋C# 綁定） | `54a2007fb`/`8cffaed3d`/`58501786c` | landed |
| RAG 權威 → xstore（ragd-rs `POSTGRESQL_RETIRED_USE_METADATA_STORE`） | `codex-postgres-rust*` 認領 | landed |
| Codex native snapshot 唯讀驗證（`NativeCodexMigration.Capture`＋`NativeCodexSql`） | `5c56c34fe` | landed（`authority_flipped:false`，讀橋） |
| Channels 原生通知傳輸（取代 LISTEN/NOTIFY） | `5a463151e`/`fe7663f0d` | landed |
| MainSystem PG readiness 退役（`POSTGRESQL_RETIRED`） | `codex-postgres-main-retirement` 認領 | landed |
| xstore metadata write plane（meta_*.rs＋NativeMetadataClient＋影子 hooks） | `devin-cli` 認領（進行中） | in-flight |
| AuditGate `psql` → 受管 `--authority-state` | 本次 | landed |

## 五、分階段路線

### Phase 0 — 盤點與證據基線（本文件）
- 消費者盤點完成；live catalog 與 registry 分歧已記錄；
- `pg_stat_activity` 觀測：無應用連線（單點觀測，非永久證明）。

### Phase 1 — 消外部工具依賴（本次）
- 移除 `psql.exe` 子行程依賴（AuditGate 改走受管 exe）；
- 後續同類：確認無其他 `psql`/pg-dump 生產子行程殘留
  （`migration-live-catalog-probe-20261002.ps1` 屬治理/開發工具，不入生產合約）。

### Phase 2 — 凍結表封存＋讀者改道
- `gptbridge_transport.outbox_event`、`gptbridge_workflow.*`：
  一次性受管匯出（沿用 NativeCodexMigration.Capture 的 repeatable-read
  read-only 模式，一般化至多 schema），產出 pinned snapshot artifact；
- backend `outbox.rs`/`saga.rs` 改讀原生快照或退役對應端點
  （兩者皆 fail-closed 設計，DSN 缺席時已自然沉默）；
- 完成後 `pg.rs` 與 `postgres` crate 可自 backend 移除（Rust 面 PG 依賴歸零）。

### Phase 3 — 工具自有資料（ai-collab 試點）
- `gptbridge_collab` 為最小 live 寫域（~150 列、單一消費者、工具自有）；
- 候選目標：xstore meta plane 或 tool-local 原生 store；
- 驗收：CRUD parity、啟動 DDL 由一次性遷移取代、無 DSN 時 fail-closed 語意不變。

### Phase 4 — 權威域（修正案執行後）
- `gptbridge_codex` 寫入路徑：PgImport/StageCodec/RepairLiveProjections/
  MigrationExecutor 的 native SQL 等價物——須 native SQL 引擎提供
  交易、原子 schema 切換、receipt 鏈；
- `gptbridge_xingcheng*` parity 驗證完成後凍結；
- 每域切換獨立取證：row count＋內容 hash＋generation pin＋回滾演練。

### Phase 5 — 移除與退役宣告
- 全部消費者遷移後：移除 Npgsql/postgres/pgx 依賴項、DSN 傳播點、
  `PgDsn`/`pg.rs`/`dsn/` 解析器、矛盾設定
  （`startup_manifest.json` 仍宣告 postgresql core-critical＋port 5432、
  `tool-runtime-contract.json` central_index、`resident-core.json`、
  `cross-language-contract.json` 中的 PG 權威字句）；
- 服務停用屬不可逆操作：須由治理明確核准後另行執行；
- 退役宣告僅在「無 runtime DSN/驅動依賴＋全部 parity 收據齊備」後成立。

## 六、開放問題

1. `NativeCodexMigration.Capture` 目前無 in-repo 呼叫點——需接線至
   遷移驅動（快照產線）才能成為 codex 讀面替代品；
2. `MirrorWriter` 對 live authority 的 `writeBack` 路徑是否曾被
   `gptbridge_runtime` 拒絕——需驗證角色授權再分類；
3. `gptbridge_audit`/`gptbridge_index` 等無消費者 schema 的最終封存
   形式（snapshot artifact vs 純治理證據檔）需治理裁決；
4. `startup_manifest.json` 的 postgresql core-critical 與
   DependencyProbes `POSTGRESQL_RETIRED` 互斥——同步順序需設計
   （先消費者遷移、再降 criticality、最後移除條目）；
5. ai-collab 無 DSN fallback 語意（exit 13）在遷移期間的等價物。

## 七、驗證與安全約束（全程）

- 來源一律唯讀（repeatable-read read-only transaction）；不得對 PG
  執行任何 DDL/DML/關庫；
- 預估列數（reltuples）僅供規劃；切換前每表須 exact count＋內容 hash；
- 不將敏感 DSN/密碼寫入原始碼、文件或提交；
- 權限/RLS 等價物須在目標引擎逐域驗證；
- 任何切換點 fail-closed；無證據不翻轉權威。
