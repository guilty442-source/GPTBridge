# [Level 1] Recovery Governance

## 1. Recovery Authority (G-077)
`RecoveryManager` 是系統唯一的恢復權威。禁止服務自行進行無限制重啟或隱藏恢復邏輯。

## 2. Recovery Escalation (G-094)
恢復流程必須有明確的升級路徑：`retry` → `degrade` → `isolate` → `disable` → `manual recovery`。禁止無限重試循環。

## 3. Runtime Recovery Priority (G-019)
發生錯誤時，優先選擇 `degrade` (降級)、`recover` (恢復) 或 `isolate` (隔離)。禁止直接導致 App 崩潰。