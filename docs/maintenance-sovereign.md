# 維護主宰職責與相關模組功能說明

> 本文件以治理法典（Governance Codex）權威條文為依據，整理維護主宰（maintenance-sovereign）的職責範圍、子主宰能力劃分、以及對應的實作模組。中文僅作備用參考；判定依據一律以法典正式權威本（`governance_rule/codex/__init__.py`）為準（A36/E22）。

---

## 1. 法典依據

### 1.1 主宰宣告

維護主宰定義於 `governance_rule/codex/sovereigns.py`：

| 欄位 | 值 |
|---|---|
| id | `maintenance-sovereign` |
| area | `maintenance` |
| rank | `top-orchestrator` |
| basis | `codex` |

### 1.2 相關法條

| 條文 | 章節 | 主旨 |
|---|---|---|
| **A24** | 六 | 一切系統維護功能歸維護主宰；職責含更新、健康監控（含資料完整性）、自動修復、故障判定、備份；禁止代行維護、越權執行 |
| **A25** | 六 | 系統健康監控歸維護主宰，含資料完整性查核與呈現；禁止忽略或掩蓋異常 |
| **A33** | 六 | 邊界劃分：資料完整性查核執行歸資料主宰；健康監控歸維護主宰（僅呈現資料完整性健康狀態）；禁止互相代行 |
| **A43** | 六 | 熱更新標的僅限受治理可執行碼；版本凍結邊界；排除 codex/data/directory/manifest |
| **A47** | 二 | 違規處置：停止→記錄→裁決；偵測由維護監控負責；裁決歸治理權威 |
| **A57** | 六 | 自我健康測試為自我維護健康所必需；歸維護主宰；須可離線收集、不依賴活模型/網路 |

### 1.3 相關法令（Edict）

| 法令 | 主旨 |
|---|---|
| **E8** | 維護主宰職責：更新/健康監控(含資料完整性)/自動修復/故障判定/備份 |
| **E6** | 熱更新：版本凍結邊界，須治理授權 |
| **E20** | 邊界：健康=維護主宰；資料完整性=資料主宰；各自獨立 |
| **E43** | 自我健康測試：歸維護主宰；受治理工具須宣告測試檔；須可離線收集 |

---

## 2. 維護主宰七大職責

維護主宰的職責（duties）由法典宣告，共七項：

| # | 職責 | 說明 |
|---|---|---|
| 1 | `update-management` | 系統/模組更新管理；監督版本凍結的熱更新邊界（決策層，不執行）；協調全系統熱重載（hot-reload） |
| 2 | `system-health-monitoring` | 系統健康監控，含運行、資源、資料完整性之健康狀態呈現 |
| 3 | `data-integrity-presentation` | 資料完整性之健康呈現（查核執行歸資料主宰，A33） |
| 4 | `automatic-repair-coordination` | 自動修復協調；隔離受損狀態、執行修復、隔離管理 |
| 5 | `fault-determination` | 系統故障/失敗判定 |
| 6 | `backup-coordination` | 備份協調；遵循治理備份政策呈報動態 |
| 7 | `self-health-test-management` | 自我健康測試管理（A57/E43） |

### 2.1 禁止事項

- `overstep-execution` — 越權執行（主宰為決策層，無執行權，A5/E2）
- `exceed-codex` — 逾越法典
- `ignore-or-hide-data-integrity-abnormal` — 忽略或掩蓋資料完整性異常（A25）
- `proxy-data-integrity-check` — 代行資料完整性查核（A33，查核執行歸資料主宰）
- `proxy-permission-matters` — 代行權限事務

---

## 3. 維護主宰底下之子主宰

維護主宰的執行能力下放給五個統一子主宰（role=sub-sovereign），定義於 `governance_rule/execution/tool_runtime/sub_sovereign.py`。五者皆隸屬維護主宰（`UNDER: ("maintenance",)`）。

| 子主宰 | authority | 職責 (duty) | 對應模組 |
|---|---|---|---|
| **自動清理** | `automatic-cleanup` | 臨時檔清理、快取清理、空目錄清理 | `global-cleaner`、`tool_local_cleanup.py` |
| **自動備份** | `automatic-backup` | 備份協調、備份完整性呈現 | `maintenance_sovereign.py` `_backup_status()` |
| **自動修復** | `automatic-repair` | 損害隔離、修復執行、隔離管理 | `tool_self_repair.py`、`main_system_self_maintenance.py` |
| **自動更新** | `automatic-update` | 更新管理、更新套用 | `maintenance_sovereign.py` `_update_status()`、HotUpdateService |
| **健康監控** | `health-monitoring` | 系統健康監控、健康狀態呈現、健康事件通知 | `maintenance_sovereign.py` `_health_monitoring()`、`core/health.py` |

---

## 4. 實作模組功能說明

### 4.1 維護主宰本體

**檔案**：`main-system/src-core/core_system/maintenance_sovereign.py`

`MaintenanceSovereign` 類別是維護主宰的程式內（in-process）實作。它是決策層，協調已注入的受治理服務，本身不執行重工作。

| 方法 | 對應職責 | 說明 |
|---|---|---|
| `start()` | 全部 | 啟動資源釋放迴圈（5 分鐘）與能力偵測迴圈（1 小時） |
| `live_status()` | 全部 | 即時狀態：更新/健康/修復/故障/備份/清理/自維護 |
| `orchestration_status()` | 全部 | 編排狀態摘要 |
| `_health_monitoring()` | 健康監控 | 呼叫 `core.health.check_core_health` + 治理完整性就緒狀態 |
| `_update_status()` | 更新管理 | 監督 HotUpdateService 的版本凍結邊界與全系統熱重載能力 |
| `execute_hot_reload()` | 更新管理 | 協調全系統熱重載（需治理授權，範圍為所有後端 src roots） |
| `_automatic_repair_status()` | 自動修復 | 協調 CentralRepairService |
| `_fault_determination_status()` | 故障判定 | 呈現修復規劃器就緒狀態 |
| `_backup_status()` | 備份協調 | 協調受治理備份/備份提取執行器 |
| `_module_cleanup_status()` | 模組清理 | 統一監督各模組的自發性清理（執行留在各模組） |
| `_main_system_self_maintenance_status()` | 主系統自維護 | 監督 `MainSystemSelfMaintenance` 受治理執行器 |
| `_run_capability_checks()` | 能力偵測 | 唯讀偵測維護相關功能/元件是否就位（不安裝、不修復） |

### 4.2 主系統自我維護服務

**檔案**：`main-system/src-core/core_system/main_system_self_maintenance.py`

`MainSystemSelfMaintenance` 是主系統自身的受治理自我維護執行器，執行三項界限內職責：

| 職責 | 實作 | 說明 |
|---|---|---|
| 原始碼自修復 | `SourceRepairService` | 針對 `main-system/src-core` |
| 本地清理 | `run_local_cleanup` | 針對 `main-system/` |
| 完整性驗證 | `GovernanceAuthenticationService.verify_runtime_integrity` | 唯讀驗證 |

觸發時機：啟動時一次 + 週期性（預設 6 小時）+ 手動（IPC `app:run-main-system-self-maintenance`）。

### 4.3 每日全域清理服務

**檔案**：`main-system/src-core/core_system/daily_global_cleaner_service.py`

`DailyGlobalCleanerService` 是主系統擁有的每日觸發器，執行留在受治理的 Global Cleaner。

| 項目 | 值 |
|---|---|
| 間隔 | 24 小時 |
| 啟動延遲 | 60 秒 |
| 回應逾時 | 30 分鐘 |
| 模組清理指令 | `toolbox_run_local_cleanup` |
| 例外模組 | `governance_rule`（治理權威樹不參與模組自清理） |

### 4.4 Global Cleaner 工具

**檔案**：`global-cleaner/src/backend/services/project_cleaner/`

`ProjectCleanupService` 繼承 `CleanupEngine`，是受治理的全域清理執行器。

| 功能 | 說明 |
|---|---|
| `clear_managed_temp_files()` | 清理受管理的臨時檔；須 `governance/main-system` 或 `governance/tool/global-cleaner` 請求者身分 |
| 清理範圍 | 僅限 `global-cleaner/runtime/temp/` 目錄 |
| 安全檢查 | 排除符號連結、reparse point；驗證 manifest.json 存在 |

### 4.5 工具本地清理（治理執行層）

**檔案**：`governance_rule/execution/tool_runtime/tool_local_cleanup.py`

`run_local_cleanup` 是各工具自發性本地清理的治理執行函數。

| 項目 | 說明 |
|---|---|
| 範圍 | 僅限工具自身根目錄；不觸及其他模組、治理資料、備份、git 追蹤內容 |
| 排除目錄 | `.git`、`.venv`、`node_modules`、`dist`、`release`、`browser-profile` 等 |
| 保護目錄 | `runtime/ipc`、`runtime/state`、`runtime/recovery`、`data/business` |
| 清理規則 | 工具臨時檔、Python bytecode (`__pycache__`)、工具快取 |

### 4.6 工具自修復（治理執行層）

**檔案**：`governance_rule/execution/tool_runtime/tool_self_repair.py`

`run_local_self_repair` 是各工具本地自修復的治理執行函數。

| 功能 | 說明 |
|---|---|
| `database_integrity()` | SQLite 完整性檢查（`PRAGMA integrity_check`） |
| `sqlite_candidates()` | 掃描 `runtime/state`、`data/business` 下的 SQLite 檔案 |
| 修復範圍 | 隔離受損 SQLite、修復 pycache 等工具本地狀態 |

### 4.7 系統救援工具

**檔案**：`system-rescue/src/backend/services/system_rescue/integration/platform_packager.py`

`system-rescue` 工具負責平台打包整合，在 main-system 治理下執行中央打包操作。

| 功能 | 說明 |
|---|---|
| `--all --verify` | 驗證所有已打包工具 |
| `--tool <id> --package` | 打包指定工具 |
| `--tool <id> --verify` | 驗證指定工具 |

### 4.8 熱重載（全系統範圍）

**檔案**：`main-system/src-core/core_system/hot_update_service.py`

`HotUpdateService` 在更新管理職責下提供全系統熱重載能力。熱重載與熱更新不同：熱重載是原地重新載入已載入的 Python 模組，不需要版本變更；熱更新是版本閘控的凍結邊界，需要版本變更與治理授權。

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

### 4.9 運行子主宰（系統主宰底下）

**檔案**：`main-system/src-core/core_system/runtime_sub_sovereign.py`

`RuntimeSubSovereign` 隸屬系統主宰（非維護主宰），但與維護密切相關：

| 職責 | 說明 |
|---|---|
| IPC 伺服器健康/就緒合約 | 確保平台存活並能服務請求 |
| 運行狀態報告 | 範圍、階段、版本 |
| 閒置記憶體維護 | 運行資源健康 |
| 熱更新邊界 | 凍結直到治理授權 |
| 治理運行完整性 | 追蹤就緒狀態 |

---

## 5. 邊界與禁止代行關係

```
┌─────────────────────────────────────────────────────────────────┐
│                    維護主宰 (maintenance-sovereign)               │
│  職責：更新/健康監控/修復協調/故障判定/備份/自健康測試             │
│  禁止：越權執行、代行資料完整性查核、代行權限、掩蓋異常            │
├─────────────────────────────────────────────────────────────────┤
│  子主宰（執行層，role=sub-sovereign）                             │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐│
│  │自動清理  │ │自動備份  │ │自動修復  │ │自動更新  │ │健康監控  ││
│  │cleanup   │ │backup    │ │repair    │ │update    │ │health    ││
│  └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘│
└─────────────────────────────────────────────────────────────────┘

邊界劃分（A33/E20）：
  資料完整性查核執行 → 資料主宰 (system-data-sub-sovereign)
  資料完整性健康呈現 → 維護主宰 (maintenance-sovereign)
  禁止互相代行
```

---

## 6. 相關檔案索引

| 類別 | 檔案路徑 |
|---|---|
| **法典權威本** | `governance_rule/codex/__init__.py` |
| **法典中文備用** | `governance_rule/codex/chinese.py` |
| **主宰宣告** | `governance_rule/codex/sovereigns.py` |
| **子主宰契約** | `governance_rule/execution/tool_runtime/sub_sovereign.py` |
| **熱更新/熱重載服務** | `main-system/src-core/core_system/hot_update_service.py` |
| **維護主宰實作** | `main-system/src-core/core_system/maintenance_sovereign.py` |
| **主系統自維護** | `main-system/src-core/core_system/main_system_self_maintenance.py` |
| **每日全域清理** | `main-system/src-core/core_system/daily_global_cleaner_service.py` |
| **運行子主宰** | `main-system/src-core/core_system/runtime_sub_sovereign.py` |
| **Global Cleaner** | `global-cleaner/src/backend/services/project_cleaner/application/service.py` |
| **工具本地清理** | `governance_rule/execution/tool_runtime/tool_local_cleanup.py` |
| **工具自修復** | `governance_rule/execution/tool_runtime/tool_self_repair.py` |
| **系統救援** | `system-rescue/src/backend/services/system_rescue/integration/platform_packager.py` |
| **核心健康檢查** | `main-system/src-core/core/health.py` |
