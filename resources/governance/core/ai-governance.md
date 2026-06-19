# [Level 1] AI 治理 (AI Governance)

本文件定義了 AI 代理在參與此專案開發時必須遵守的規範，確保 AI 協作的透明性、可控性與治理一致性。

## 1. AI 修復邊界 (AI Patch Boundary - G-020)

- AI 執行修復時，必須遵循「最小影響範圍」。禁止為了修復單一 Bug 而大範圍重構不相關的架構層。
- 修改必須維持最小影響範圍，明確列出受影響模組與依賴衝擊。
- 任何 Workaround 必須明確標註 `TEMPORARY`、建立時間、原因及移除條件。
- **必須優先參考 `resources/governance/GOVERNANCE_INDEX.md`。**

## 2. AI 治理邊界 (AI Governance - G-078, G-080, G-081)

- **禁止架構漂移 (Architecture Drift Prevention)**: 不得新增平行架構、Runtime 或第二套啟動流。
- **修改預算 (Change Budget - G-056)**: 每次修改應限制受影響檔案數量與模組跨度。
- **回滾安全 (Rollback Safety)**: 重大修改前需產出 Impact Report，並確認有備份/回滾點。
- **漂移檢測 (Drift Detection)**: 治理系統會自動檢測 Alias、路徑或邏輯漂移。
- **範圍約束 (Scope Containment - G-078)**: AI 僅能修改被授權的範圍（Scope），禁止範疇蠕變（Scope Creep）或未經授權的跨層重構。
- **結構鎖定 (Structural Drift Lock - G-080)**: AI 絕對禁止修改 Alias、Startup、Runtime 與 Governance 的權威邏輯，除非有顯式的架構遷移指令。
- **治理 DNA (Governance Compliance)**: 任何修改必須優先符合 `GOVERNANCE_INDEX.md` 中的規則。
- **AI Mutation Visibility (G-081)**: AI 的修改必須完全可觀測（Observable），包含檔案、依賴、架構與治理規則的差異（Delta）報告。

## 3. 驗證規範 (Validation Rules)

- **預執行**: 執行指令前需通過 `CommandPolicy` 評估。
- **方法**: 針對性驗證修改內容。
- **完整性**: 定期執行 `npm run build`。

## 4. 報告規範 (Reporting Rules)

- 重大修改必須記錄於 `OptimizationHistoryManager`。
- 錯誤需詳細記錄 Exception 與逾時資訊。
- 每次修改必須產出「Governance Alignment Report」。

## 5. 執行風格 (Execution Style)

- **小規模執行**: 每次任務最多處理 3 個檔案。
- **並行限制**: 僅支援序列處理 (Serial only)。
- **非同步要求**: 強制使用 `async/await`。
- **API 限制**: 禁止直接調用 AI API (僅限使用瀏覽器自動化模式)。
- **程式碼品質**: Python 必須標註型別，TypeScript 必須定義介面。
  - **UI 語系**: 必須使用繁體中文。
  - **命名規範**: 程式碼邏輯與變數命名強制使用英文。
- **重構限制**: 禁止未經請求的重構。

## 6. 範圍規範 (Scope Rules)

- **優先級**: 僅存取任務相關檔案。
- **搜尋限制**: 禁止遞迴搜尋，除非有 `full audit` 指令。

## 7. 禁止路徑 (Forbidden Paths)

- **目錄**: `node_modules`, `.venv`, `runtime`, `dist`, `logs`, `build` 等產出路徑。
- **副檔名**: `.lock`, `.map`, `.exe`, `.dll`, `.zip` 等二進位或暫存檔。

## 8. 審計規則 (Audit Rules)

- 禁止未經授權的全專案掃描。
- 需收到 `full audit` 指令才執行全面審計。