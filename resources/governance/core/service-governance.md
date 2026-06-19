# [Level 1] Service Governance

## 1. Service Dependency Governance (G-037)
服務之間的依賴必須顯式聲明。禁止透過全域變數或隱藏路徑進行秘密調用。

## 2. Service Crash Containment (G-027)
單一服務（如 WebSocket 或 Plugin）崩潰時，其影響範圍必須被限制在該服務單元內，不得導致整個 Renderer 或 App 崩潰。

## 3. Async Governance (G-024)
所有異步操作（Promises）必須包含 `try-catch` 塊與超時（Timeout）保護。禁止出現未處理的 `unhandledRejection`。

## 4. Recovery Loop Prevention (G-026)
系統必須偵測並阻止「無限重啟循環」。若服務在 30 秒內重啟超過 3 次失敗，應強制進入停止狀態並標記 `FAIL`。

## 5. Memory Governance (G-038)
服務必須定期自我檢查內存佔用。長駐服務需提供 `dispose()` 方法以徹底釋放資源。

## 7. Cascading Failure Isolation (G-074)
任何服務失效（Service Failure）絕對禁止級聯（Cascade）影響到不相關的模組。必須實作獨立隔離、降級與恢復機制。