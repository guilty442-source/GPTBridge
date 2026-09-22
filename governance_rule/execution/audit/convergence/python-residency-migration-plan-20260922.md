# 主系統 Python 常駐外移計畫（P0 §1.1 目標二）

> 對應藍圖 `P0 | **主系統 Python 常駐外移**` 細項 ①-④
> 現狀：`main-system/config/resident-core.json:1`（`star-resident-core/v1`）已定義最小常駐集（IPC/治理閘門/runtime state/request registry/periodic scheduler/核心主宰/activation broker/maintenance/watchdog/outbox/authority reanchor）；`hot_reload_watcher` 已改 on-demand（`ea6372e6`），R3 後固定週期 5 項、RSS p95 82.6 MB、ready 1.72s。

## ① 常駐職責清單化

已落地：`resident-core.json` 為單一權威；`startup_core/resident_core.py:1` 為 loader（fail-safe：未知元件預設 resident）。需補齊對照表：

| 職責 | 現宿主 | 目標宿主 | 狀態 |
| --- | --- | --- | --- |
| IPC/治理閘門/runtime state/request registry | Python `main-system` | 治理主宰（Python）+ C 高速核心 IPC | 已在 resident-core，待外移評估 |
| periodic scheduler | Python | C Event Loop + Task Queue | 原型可實作（§1.1 C 原型） |
| hot_reload_watcher | Python | on-demand（已完成） | done |
| RAG/CAG/DAG、local-model、ollama、tools | Python | on-demand（已完成 lazy） | done |
| 業務編排（模型選擇/任務調度/生命週期/資源預算/狀態） | Python | C# `business-logic-csharp`（P7） | P7-2 sidecar 已起，P7-3 待演練 |

## ② 外移順序與替代宿主

1. **C 高速核心原型**（`native/core`，純 C）：Event Loop / Task Queue / Lifecycle SM / Resource Monitoring / Deadline-Cancellation / IPC（§1.1 表格）— 獨立建置、單元測試綠、與 Python 治理主宰經穩定 C ABI 介接，fail-closed 仍在 Python。
2. **C# 業務編排**（P7）：P7-1 契約盤點 → P7-2 sidecar 新功能先行 → P7-3 生命週期/預算/狀態主導權移轉 → P7-4 Python 退為訓練與匯出。
3. **資訊層**（`shared-layer`）保持 `resident`（`architecture_registry.json:information/standalone-service/resident/canonical`），不得直接刪除或搬移（A8/A44/A49/A263）；變更走契約版本軸。

## ③ 過渡雙軌與回退

- 雙軌：Python 治理主宰與 C 高速核心並存，同一請求不得同時受 A334 固定分配與 A592 能力匹配控制（`PRE_TRANSITION→ACTIVATED` 單一模型原則）。
- 回退：C 原型或 C# 側失敗時回退至 Python 現行路徑（`auto_activate` 前 fail-closed，保留原權重/服務）。
- 驗證：等價性測試（logits ≤1e-3、greedy 一致）、審計事件齊備、資源預算可稽核。

## ④ 驗收

- ready ≤3.0s / phase-5 ≤600ms / RSS p95 ≤220 / idle CPU ≤3% / 固定週期 ≤6（已達標，見 §10.63 R0-R5）
- 零功能退化：治理稽核 PASS、SLA（啟動 10s/測試 20s/審計 30s/工具啟停 5s）不回退、fail-closed 不變
- 整體淨佔用：不得以 Python 省 100 MB 換 C# / C++ 多 150 MB（§1.1 2026-09-21 核定）

## 下一步

- 依 §10.31/10.32 受治理轉型狀態機推進：`PREPARED→PARITY_VERIFIED→CUTOVER_READY→ACTIVATED→LEGACY_RETIRED`，每步附 `sovereign_transition_registry.json:1` 證據。
- 本計畫為派生文件，不改法典效力；正式外移需法典 A591/A592 `proposed→active` 與 `architecture_registry` 切換。
