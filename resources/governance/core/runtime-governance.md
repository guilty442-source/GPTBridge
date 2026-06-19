# [Level 1] Runtime Governance

## 1. One Truth Per System (G-048)
每個系統狀態或邏輯必須有且僅有一個權威來源。禁止在不同模組中重複維護相同狀態。

## 2. Canonical Runtime Ownership (G-021)
系統運行階段由 `RuntimeServiceManager` 與 `MainShell` 共同擁有。

## 8. Runtime Authority Singularity (G-068)
禁止重複的啟動編排器、服務登錄器或事件匯流排。全系統只能存在一套正式的 Runtime Authority。

## 3. App Shell Stability (G-031)
App Shell (UI) 必須獨立於後端服務存活。後端崩潰或未啟動時，UI 必須渲染降級介面而非白屏或永久 Loading。

## 4. Controlled Degraded Mode (G-044)
服務失敗時，必須明確顯示「受限模式」UI，並提供救援（Rescue）或重試（Retry）入口。

## 9. Startup Deadlock Prevention (G-076)
啟動流程（Startup）禁止循環依賴、隱藏的 await 鏈或巢狀啟動門禁（Nested Startup Gate）。

## 10. Recovery Authority (G-077)
`RecoveryManager` 是系統唯一的恢復權威，禁止服務自行進行無限制重啟。