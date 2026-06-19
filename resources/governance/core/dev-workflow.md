# [Level 3] Development Workflow Governance

## 1. Execution Style
- **小規模執行**: 每次任務最多處理 3 個檔案。
- **並行限制**: 僅支援序列處理 (Serial only)。
- **非同步要求**: 強制使用 `async/await`。
- **API 限制**: 禁止直接調用 AI API (僅限使用瀏覽器自動化模式)。
- **程式碼品質**: Python 必須標註型別，TypeScript 必須定義介面。
  - **UI 語系**: 必須使用繁體中文。
  - **命名規範**: 程式碼邏輯與變數命名強制使用英文。
- **重構限制**: 禁止未經請求的重構。