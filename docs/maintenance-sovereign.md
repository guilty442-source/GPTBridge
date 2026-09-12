# 維護主宰職責與相關模組功能說明

> **重要：維護主宰已退役（A302/A323）**。本文件保留為歷史追溯參考。
> 維護主宰的職責已移至 **健康維護測試子主宰**（health-maintenance-test-sub-sovereign），
> 隸屬決策主宰（decision-sovereign）。自動清理/備份/修復/更新已移至對應的
> 同步子主宰（cleanup-retention / repair-backup / release-update 等，A322）。
> 現行正典身分請查法典權威本（`governance_rule/codex/data/governance_codex.sqlite3`）。
>
> 本文件以治理法典（Governance Codex）權威條文為依據，整理維護主宰（maintenance-sovereign）的職責範圍、子主宰能力劃分、以及對應的實作模組。中文僅作備用參考；判定依據一律以法典正式權威本（`governance_rule/codex/__init__.py`）為準（A36/E22）。
>
> **法典修正說明**：法典已透過 H009（v1.52010「maintenance narrowed」）與 H014（v1.73120）修正。維護主宰從原本的「top-orchestrator」（擁有 update/repair/fault/backup）收窄為「specialized-decision-sovereign」（聚焦 system-health）。舊條文 A24/E8/A67/A72/E48/E52 均已 superseded，由 A125/E102/A152/A154/E127/E128 取代。隨後 A302（v1.11000）將維護主宰退役，職責移至健康維護測試子主宰（隸屬決策主宰）；A323 進一步細分決策主宰底下的子主宰。

---

## 1. 法典依據

### 1.1 主宰宣告（已退役 — A302/A323）

> **退役宣告**：維護主宰（maintenance-sovereign）已依 A302 退役。
> 其法典記錄保留為歷史血脈（rank 標記為 retired）。
> 現行正典身分為 **健康維護測試子主宰**（health-maintenance-test-sub-sovereign），
> 隸屬決策主宰（decision-sovereign），area=maintenance。

維護主宰定義於法典權威本（`governance_rule/codex/__init__.py` → SQLite 資料庫），現為退役記錄：

| 欄位 | 值 |
|---|---|
| id | `maintenance-sovereign` |
| name | `system-health-sovereign` |
| area | `maintenance` |
| rank | `specialized-decision-sovereign` |
| basis | `codex` |
| duties | `monitor-system-health`, `preserve-system-health`, `maintain-system` |
| powers | `declare-system-health-state`, `dispatch-governed-health-maintenance`, `accept-health-restoration` |
| prohibitions | `direct-ungoverned-execution`, `cross-sovereign-duty-takeover`, `permission-self-authorization` |

### 1.2 相關法條（有效條文）

| 條文 | 章節 | 主旨 |
|---|---|---|
| **A125** (取代 A24) | 六 | 維護主宰職責收窄為 monitor/preserve/maintain system-health；委派 governed-maintenance-executors；禁止非維護域所有權與直接未治理執行 |
| **A25** | 六 | 系統健康監控歸維護主宰，含資料完整性查核與呈現；禁止忽略或掩蓋異常 |
| **A33** | 六 | 邊界劃分：資料完整性查核執行歸資料主宰；健康監控歸維護主宰（僅呈現資料完整性健康狀態）；禁止互相代行 |
| **A43** | 六 | 熱更新標的僅限受治理可執行碼；版本凍結邊界；排除 codex/data/directory/manifest |
| **A57** | 六 | 自我健康測試為自我維護健康所必需；歸維護主宰；須可離線收集、不依賴活模型/網路 |
| **A152** (取代 A67) | 六 | 前後端恢復邊界：HEALTH-OWNER 維護主宰；REPAIR-DECISION 決策主宰；RUNTIME-RESTART 系統運行主宰；CODE-CHANGE 系統程式主宰；LEARNING 學習決策主宰；禁止維護主宰擁有非健康決策 |
| **A154** (取代 A72) | 六 | 修復責任鏈：維護範圍僅限健康；修復決策歸決策主宰；禁止維護改碼、維護權限、平行所有者、無證變更 |

### 1.3 相關法令（Edict，有效條文）

| 法令 | 主旨 |
|---|---|
| **E102** (取代 E8) | 維護主宰職責=monitor/preserve/maintain system-health；一切特定維護動作從屬此三職責並委派 governed-executors |
| **E20** | 邊界：健康=維護主宰；資料完整性=資料主宰；各自獨立 |
| **E43** | 自我健康測試：歸維護主宰；受治理工具須宣告測試檔；須可離線收集 |
| **E127** (取代 E48) | 運行恢復：HEALTH 維護；REPAIR-DECISION 系統決策；RUNTIME-ACTION 系統運行；CODE-ACTION 系統程式；LEARNING 學習系統；UI 即時 |
| **E128** (取代 E52) | 修復鏈：健康信號>維護分類>系統決策>權限>運行或程式>執行器>驗證>資訊層>UI |

### 1.4 已廢止條文（superseded，僅作追溯參考）

| 舊條文 | 取代者 | 廢止版本 |
|---|---|---|
| A24 | A125 | v1.52010 |
| E8 | E102 | v1.52010 |
| A67 | A152 | v1.73120 |
| A72 | A154 | v1.73120 |
| E48 | E127 | v1.73120 |
| E52 | E128 | v1.73120 |

---

## 2. 維護主宰三大職責

維護主宰的職責（duties）由法典宣告（A125/E102），共三項，均聚焦系統健康：

| # | 職責 | 說明 |
|---|---|---|
| 1 | `monitor-system-health` | 系統健康監控，含運行、資源、資料完整性之健康狀態呈現 |
| 2 | `preserve-system-health` | 維護系統健康（備份協調、健康恢復接受） |
| 3 | `maintain-system` | 維持系統健康（委派清理、更新邊界監督） |

### 2.1 權力（powers）

| 權力 | 說明 |
|---|---|
| `declare-system-health-state` | 宣告系統健康狀態 |
| `dispatch-governed-health-maintenance` | 派發受治理健康維護 |
| `accept-health-restoration` | 接受健康恢復 |

### 2.2 禁止事項（prohibitions）

- `direct-ungoverned-execution` — 直接未治理執行
- `cross-sovereign-duty-takeover` — 跨主宰職責接管
- `permission-self-authorization` — 權限自我授權
- `maintenance-owning-non-health-decisions` — 維護主宰擁有非健康決策（A152）
- `maintenance-code-change` — 維護主宰改碼（A154）
- `maintenance-permission` — 維護主宰處理權限（A154）
- `ignore-or-hide-data-integrity-abnormal` — 忽略或掩蓋資料完整性異常（A25）
- `proxy-data-integrity-check` — 代行資料完整性查核（A33）

---

## 3. 職權邊界劃分（A152/A154/E127/E128）

法典修正後，原本由維護主宰擁有的非健康職責已移至其他主宰：

| 職權 | 擁有者 | 法典依據 |
|---|---|---|
| 系統健康（監控/維護/維持） | **健康維護測試子主宰** (health-maintenance-test-sub-sovereign) | A302/A323 |
| 修復決策 | **決策主宰** (decision-sovereign) | A152/E127 |
| 運行動作（熱重載/重啟） | **運行主宰** (runtime-sovereign) | A300/E127 |
| 程式碼變更 | **發布更新同步子主宰** (release-update-sync-sub-sovereign) | A309/A322 |
| 錯誤學習 | **學習證據同步子主宰** (learning-evidence-sync-sub-sovereign) | A310/A322 |
| 第三方軟體更新 | **依賴同步子主宰** (dependency-sync-sub-sovereign) | A307/A322 |
| 資料完整性查核 | **資料治理子主宰** (data-governance-sub-sovereign) | A304/A323 |

### 3.1 修復決策鏈（E128）

```
健康信號 > 維護主宰分類(health-classification) > 決策主宰(修復決策)
> 權限驗證 > 系統運行或系統程式(派發) > 受治理執行器 > 獨立驗證
> 資訊層狀態事件審計 > UI 同步
```

- 維護主宰：分類健康信號（health-only），不做修復決策
- 決策主宰：做修復決策，路由至權限→運行/程式→執行器→驗證
- 維護主宰：記錄結果至學習庫（E127 learning-system），同步 UI

---

## 4. 維護主宰底下之子主宰（已退役 — A302/A322/A323）

> **退役宣告**：維護主宰已退役（A302），其底下之子主宰角色常數保留於
> `governance_rule/execution/tool_runtime/sub_sovereign.py` 作為相容性別名。
> 自動清理/備份/修復/更新已移至同步主宰底下的單一職責同步子主宰（A322）：
> cleanup-retention-sync / repair-backup-sync / release-update-sync。
> 健康監控已移至健康維護測試子主宰（隸屬決策主宰，A323）。

原維護主宰的執行能力下放給五個統一子主宰（role=sub-sovereign），定義於 `governance_rule/execution/tool_runtime/sub_sovereign.py`。五者皆隸屬維護主宰（`UNDER: ("maintenance",)`）。現已退役，僅保留相容性常數。

| 子主宰 | authority | 職責 (duty) | 對應模組 |
|---|---|---|---|
| **自動清理** | `automatic-cleanup` | 臨時檔清理、快取清理、空目錄清理 | `global-cleaner`、`tool_local_cleanup.py` |
| **自動備份** | `automatic-backup` | 備份協調、備份完整性呈現 | `maintenance_sovereign.py` `_backup_status()` |
| **自動修復** | `automatic-repair` | 損害隔離、修復執行、隔離管理 | `tool_self_repair.py`、`main_system_self_maintenance.py` |
| **自動更新** | `automatic-update` | 更新管理、更新套用 | `maintenance_sovereign.py` `_update_status()`、HotUpdateService |
| **健康監控** | `health-monitoring` | 系統健康監控、健康狀態呈現、健康事件通知 | `maintenance_sovereign.py` `_health_monitoring()`、`core/health.py` |

> **注意**：子主宰的執行能力從屬於維護主宰的三大健康職責（E102）。修復「決策」與程式碼「變更」不在維護主宰範圍內（A152/A154）。

---

## 5. 實作模組功能說明

### 5.1 維護主宰本體（已退役 — 相容別名）

**檔案**：`main-system/src-core/core_system/maintenance_sovereign.py`（相容 shim）
**現行實作**：`main-system/governance/sub-sovereigns/health_maintenance_test_sub_sovereign.py`

`MaintenanceSovereign` 類別是維護主宰的程式內（in-process）實作，現為相容別名，指向健康維護測試子主宰（HealthMaintenanceTestSubSovereign）。原為決策層（health-only），協調已注入的受治理服務，本身不執行重工作。

| 方法 | 對應職責 | 說明 |
|---|---|---|
| `start()` | 全部 | 啟動資源釋放迴圈（5 分鐘）與能力偵測迴圈（1 小時）+ 健康分類迴圈 |
| `live_status()` | 全部 | 即時狀態：更新/健康/修復/故障/備份/清理/自維護（唯讀健康監控） |
| `orchestration_status()` | 全部 | 編排狀態摘要 |
| `_health_monitoring()` | 健康監控 | 呼叫 `core.health.check_core_health` + 治理完整性就緒狀態 |
| `_update_status()` | 維持系統 | 唯讀監督 HotUpdateService 的版本凍結邊界（執行在 system-runtime） |
| `_automatic_repair_status()` | 維護健康 | 唯讀監控修復服務就緒（決策在 decision） |
| `_fault_determination_status()` | 健康分類 | 呈現修復決策鏈就緒狀態（決策在 decision） |
| `_backup_status()` | 維護健康 | 唯讀監控受治理備份就緒 |
| `_module_cleanup_status()` | 維持系統 | 統一監督各模組的自發性清理（執行留在各模組） |
| `_main_system_self_maintenance_status()` | 維持系統 | 監督 `MainSystemSelfMaintenance` 受治理執行器 |
| `_run_capability_checks()` | 能力偵測 | 唯讀偵測維護相關功能/元件是否就位（不安裝、不修復） |

### 5.2 修復決策鏈（決策主宰）

**檔案**：`main-system/src-core/core_system/repair_decision_chain.py`

`RepairDecisionChain` 是決策主宰（`DecisionSovereignService`）的修復決策鏈實作，承擔 A152/A154/E127/E128 所規定的修復決策職責。

| 方法 | 說明 |
|---|---|
| `decide_and_route(classified_signal)` | 修復決策入口：決策→權限驗證→派發系統程式→獨立驗證 |
| `_decide_repairable()` | 修復決策：判斷是否可修復（策略、dev-mode） |
| `_validate_permission()` | 權限驗證：呼叫權限主宰授權 |
| `_dispatch_to_programming()` | 派發系統程式主宰執行程式碼變更 |
| `_verify_independent()` | 獨立驗證：從磁碟重新編譯驗證 |

### 5.3 健康分類鏈（維護主宰）

**檔案**：`main-system/src-core/core_system/maintenance_repair_chain.py`

`MaintenanceRepairChainMixin` 是維護主宰的健康分類鏈，承擔 health-only 範圍的健康信號分類，並委派修復決策至決策主宰。

| 方法 | 說明 |
|---|---|
| `_repair_decision_loop()` | 輪詢 `repair-requests.json`，分類待處理健康信號 |
| `_classify_health_signal()` | 健康分類：識別錯誤類型、目標檔案（唯讀，不做決策） |
| `_handle_repair_request()` | 分類→委派 decision→認可→學習審計→UI 同步 |
| `_audit_repair_outcome()` | 記錄修復結果至學習庫（E127 learning-system） |
| `_sync_ui_repair_result()` | 通知 UI 修復結果 |

### 5.4 熱重載（系統運行主宰）

**檔案**：`main-system/src-core/core_system/runtime_sub_sovereign.py`

Per E127（`RUNTIME-ACTION:system-runtime`），熱重載入口已移至 `RuntimeSubSovereign`：

| 方法 | 說明 |
|---|---|
| `execute_hot_reload()` | 協調全系統熱重載（需治理授權，範圍為所有後端 src roots） |

### 5.5 主系統自我維護服務

**檔案**：`main-system/src-core/core_system/main_system_self_maintenance.py`

`MainSystemSelfMaintenance` 是主系統自身的受治理自我維護執行器，執行三項界限內職責：

| 職責 | 實作 | 說明 |
|---|---|---|
| 原始碼自修復 | `SourceRepairService` | 針對 `main-system/src-core` |
| 本地清理 | `run_local_cleanup` | 針對 `main-system/` |
| 完整性驗證 | `GovernanceAuthenticationService.verify_runtime_integrity` | 唯讀驗證 |

觸發時機：啟動時一次 + 週期性（預設 6 小時）+ 手動（IPC `app:run-main-system-self-maintenance`）。

### 5.6 每日全域清理服務

**檔案**：`main-system/src-core/core_system/daily_global_cleaner_service.py`

`DailyGlobalCleanerService` 是主系統擁有的每日觸發器，執行留在受治理的 Global Cleaner。

### 5.7 Global Cleaner 工具

**檔案**：`global-cleaner/src/backend/services/project_cleaner/`

`ProjectCleanupService` 繼承 `CleanupEngine`，是受治理的全域清理執行器。

### 5.8 工具本地清理（治理執行層）

**檔案**：`governance_rule/execution/tool_runtime/tool_local_cleanup.py`

`run_local_cleanup` 是各工具自發性本地清理的治理執行函數。

### 5.9 工具自修復（治理執行層）

**檔案**：`governance_rule/execution/tool_runtime/tool_self_repair.py`

`run_local_self_repair` 是各工具本地自修復的治理執行函數。

### 5.10 熱重載服務（全系統範圍）

**檔案**：`main-system/src-core/core_system/hot_update_service.py`

`HotUpdateService` 在運行動作職責下提供全系統熱重載能力。熱重載入口由 `RuntimeSubSovereign` 擁有（E127）。

| 項目 | 值 |
|---|---|
| 範圍 | 全系統（10 個後端 src roots） |
| 治理授權 | 必需（`governance.authorize_hot_update`） |
| 標的限制 | 僅受治理可執行程式碼（Python 模組） |
| 不可重載 | 法典、資料、權限目錄、封印清單（A43/E29） |
| 不可重載模組 | `governance_rule.codex.*`、`governance_rule.permission_directory.*`、`core_system.hot_update_service` |

全系統後端 src roots：

```
main-system/src-core, shared-layer/src, local-model/src,
ai-collaboration/src, ai-assistant/src, global-cleaner/src,
system-rescue/src, file-sorter/src, vaultly/src, investment-mobile/src
```

---

## 6. 邊界與禁止代行關係

```
┌─────────────────────────────────────────────────────────────────┐
│              維護主宰 (maintenance-sovereign)                     │
│  職責：monitor/preserve/maintain system-health (health-only)      │
│  權力：declare-health-state/dispatch-health-maintenance/          │
│        accept-health-restoration                                  │
│  禁止：越權執行、跨主宰職責接管、權限自我授權、                       │
│        擁有非健康決策、改碼、處理權限、掩蓋異常                       │
├─────────────────────────────────────────────────────────────────┤
│  子主宰（執行層，role=sub-sovereign）                             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐│
│  │自動清理  │ │自動備份  │ │自動修復  │ │自動更新  │ │健康監控  ││
│  │cleanup   │ │backup    │ │repair    │ │update    │ │health    ││
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘│
└─────────────────────────────────────────────────────────────────┘

修復決策鏈（E128）：
  健康信號 > 維護主宰分類 > 決策主宰(決策) > 權限
  > 系統運行/系統程式(派發) > 執行器 > 驗證 > 資訊層 > UI

職權邊界（A152/A154/E127）：
  系統健康          → 維護主宰 (maintenance-sovereign)
  修復決策          → 決策主宰 (decision-sovereign)
  運行動作(熱重載)  → 系統運行主宰 (system-runtime-sovereign)
  程式碼變更        → 系統程式主宰 (system-programming-sovereign)
  錯誤學習          → 學習決策主宰 (learning-system-sovereign)
  資料完整性查核    → 資料主宰 (data-sovereign) [A33/E20]
  禁止互相代行
```

---

## 7. 相關檔案索引

| 類別 | 檔案路徑 |
|---|---|
| **法典權威本** | `governance_rule/codex/__init__.py` |
| **法典中文備用** | `governance_rule/codex/governance_codex.zh-TW.txt` |
| **主宰宣告** | 法典 SQLite `sovereigns` 資料表 |
| **子主宰契約** | `governance_rule/execution/tool_runtime/sub_sovereign.py` |
| **熱更新/熱重載服務** | `main-system/src-core/core_system/hot_update_service.py` |
| **維護主宰實作** | `main-system/src-core/core_system/maintenance_sovereign.py` |
| **維護主宰生命週期** | `main-system/src-core/core_system/maintenance_lifecycle.py` |
| **維護主宰狀態** | `main-system/src-core/core_system/maintenance_status.py` |
| **維護主宰健康監控面** | `main-system/src-core/core_system/maintenance_update.py` |
| **維護主宰能力偵測** | `main-system/src-core/core_system/maintenance_capability.py` |
| **維護主宰學習** | `main-system/src-core/core_system/maintenance_learning.py` |
| **維護主宰健康分類鏈** | `main-system/src-core/core_system/maintenance_repair_chain.py` |
| **修復決策鏈（決策主宰）** | `main-system/src-core/core_system/repair_decision_chain.py` |
| **決策主宰** | `main-system/src-core/core_system/decision_sovereign.py` |
| **運行子主宰（熱重載入口）** | `main-system/src-core/core_system/runtime_sub_sovereign.py` |
| **主系統自維護** | `main-system/src-core/core_system/main_system_self_maintenance.py` |
| **每日全域清理** | `main-system/src-core/core_system/daily_global_cleaner_service.py` |
| **修復協調器** | `main-system/src-core/tasks/repair_coordinator.py` |
| **熱重載監視器** | `main-system/src-core/tasks/hot_reload_watcher.py` |
| **IPC 處理器** | `main-system/src-core/ipc/handlers.py` |
| **Global Cleaner** | `global-cleaner/src/backend/services/project_cleaner/application/service.py` |
| **工具本地清理** | `governance_rule/execution/tool_runtime/tool_local_cleanup.py` |
| **工具自修復** | `governance_rule/execution/tool_runtime/tool_self_repair.py` |
| **核心健康檢查** | `main-system/src-core/core/health.py` |
