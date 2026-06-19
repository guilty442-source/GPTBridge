# [Level 1] 程式碼拆分與高風險模組保護

## 一、拆分原則

### 允許項目

- 真實的檔案搬移。
- 真實拆分過大的模組。
- 整合重複的業務邏輯。
- 修正 Import Path。
- 命名整理與分層整理。
- Shared 邏輯抽離。

### 禁止項目 (十八、禁止佔位符污染規則)

- 嚴禁為了符合目錄結構建立 **Placeholder** 組件或檔案。
- 嚴禁建立 `pass` 或 `no-op` 的假 Class/Service。
- 嚴禁建立空的 `apiClient` 或假路由。
- 嚴禁使用「預期通過」等假設性回報。
- 嚴禁建立 Fake Provider 或 Fake PatchEngine。
- **若缺少功能，只允許：回報缺少、建立 TODO skeleton (需明確標示未完成)。禁止偽裝成可用。**

## 二、真實功能保護規則 (二十、真實功能保護規則)

1. **禁止為了符合架構而犧牲真實功能。**
2. 既有真實功能禁止被 Placeholder、簡化版、TODO-only shell 或假重寫取代。
3. 整理結構時必須保留：原功能、原邏輯、原 Workflow 與原 Runtime 行為。

## 三、結構整理邊界規則 (二十一、結構整理邊界規則)

整理結構時，**禁止順手修改**：

- Provider Workflow / AI Dispatch。
- IPC 行為 / Command Handler 邏輯。
- State Machine / Orchestrator。
- Sandbox 與 Build 工作流。

## 四、高風險模組保護區

以下模組屬於系統核心，**禁止隨意重寫或以假實作替換**：

- `provider`, `orchestrator`, `ipc`, `patch_engine`, `governance`, `backup_manager`, `task_queue`, `runtime`, `browser session`, `AI dispatch`, `sandbox workflow`

**對上述模組僅允許**：搬移、Import 修正、不改動邏輯的小型拆分、型別 (Type) 修正。

## 五、先報告後執行規則 (十九、先報告後執行規則)

凡涉及搬移、刪除檔案、拆分/合併核心模組、大量 Import 修正或核心結構變更，必須先：

1. **Audit** (審計目前狀態)。
2. **風險評估** (列出風險與受影響檔案)。
3. **列出建議方案**。
**未經批准，禁止直接修改。**

## 六、失敗回滾規則 (二十二、失敗回滾規則)

1. 若整理後出現 Build/Dev 失敗、UI 白屏、Runtime/IPC 崩潰或 Provider 失效，**必須優先回滾該次整理造成的變更**。
2. 禁止在故障狀態下繼續疊加更多結構改動。

## 七、真實驗證規則 (二十三、真實驗證規則)

**禁止使用「預期通過」、「理論可行」等描述。** 必須：

1. 真正執行 `npm.cmd run build`。
2. 真正執行 `npm.cmd run dev`。
3. 真正檢查 UI、Console 與 Runtime 狀態。

## 八、TODO 規範

若功能尚未實作，必須明確標記 `TODO: implement xxx`，不得偽裝成已完成。
