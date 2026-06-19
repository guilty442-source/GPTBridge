# [Level 3] Audit Workflow Governance

## 1. Audit Rules
- 禁止未經授權的全專案掃描。
- 需收到 `full audit` 指令才執行全面審計。

## 2. Reporting Rules
- 重大修改必須記錄於 `OptimizationHistoryManager`。
- 錯誤需詳細記錄 Exception 與逾時資訊。
- 每次修改必須產出「Governance Alignment Report」。