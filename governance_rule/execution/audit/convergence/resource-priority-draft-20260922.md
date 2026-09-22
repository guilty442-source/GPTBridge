# 資源優先序草案（P0 §10.64 ③）

> 來源：藍圖 §1.1 工作期間資源約束、§10.64 控制律、法典 A334/A591/A592 權責邊界
> 性質：派生草案（治理審定前不具法典效力，僅供 amendment 參考）
> 產生：2026-09-22，`scripts/verify-10_64-engine-gpu.py:1` 驗證通過為前提

## 優先序（高→低）

1. **治理平面**（法典、權限、稽核、健康檢查、Execution Lease、租約交接）— **不得為降載停用**，永不進入 `resource-governor` 的 worker-plane 管制；`_classify_plane:governance` 永久豁免（`test_governance_plane_never_regulated`）。
2. **核心執行平面**（C 高速執行核心、按需引擎的 in-flight 工作、持有強引用的推論）— 僅在閒置超時或壓力釋放時按安全規則釋放，完成前不中斷。
3. **工作者平面**（AI 代理、toolbox 工具、測試 worker、按需 Python 訓練）— 受 `WORKER_CPU_BUDGET 10% / WORKER_RAM_BUDGET 30%` 總帳管制，超標時依序：降優先 → worker-plane affinity 上限 → WorkingSet 修剪 → 暫停非必要週期 → admission-hold（`GOVERNANCE_AUTHORITY_APPROVAL` 以外拒新工作，fail-closed）。
4. **背景維護**（RAG parity sweep、reconcile、retention 清理、daily cleaner）— 於 `PeriodicScheduler:pausable` 實現，管制中延後執行；`resource-governor` 的 `regulated` 期內 `pausable` jobs 自動暫停。

## 不變式

- **治理永不降載**：任何資源預算超標不得停用、降優先或限制法典/權限/稽核/健康檢查；`governance` plane 在 `govern_once:347` 被跳過。
- **in-flight 不中斷**：持有強引用的引擎/任務完成後才釋放（`AutoReleaseManager:hold strong ref`）。
- **Fail-closed**：`GpuCoordinator:95%` 上限與 `TimeoutError`、工作者 `admission-hold` 皆 fail-closed；`GENERAL` 預算超標不自動殺行程，僅降載。
- **可觀測**：每週期 `sample` 寫入 `resource-governor.jsonl`，p95 審計以 `scripts/verify-10_64-resource-budget.py:1` 為源；`resource-priority-draft` 本文件僅為草案，正式效力需法典 amendment。

## 變更權屬

- 預算值（10%/30%）、優先序定義的變更屬 **governor / 受權者**（`runtime/settings/self-learning.json` 同級受管設定），星澄不得自改（§2.7 觸發政策同理，`enabled=false` kill switch 永久保留）。

## 驗證

- `scripts/verify-10_64-resource-budget.py --window 90` → `window 90 full true / cpu_p95 0.5 / ram_p95 0.14 PASS`（`10_64-evidence-20260922.json:1`）
- `scripts/verify-10_64-engine-gpu.py:1` → `auto_release idle PASS / gpu status 6144/3839/2164 / can_acquire 100 PASS / VRAM limit enforced`
- `main-system/tests/test_resource_governor.py:145` → 5 passed（含 `test_governance_plane_never_regulated`）

## 後續

- 法典側需將「治理>核心>工作>維護」與「不得為降載停用治理」寫入正式條文（amendment），本草案屆時以 `successor` 明文下放並重建 `runtime-rule-index` 與 `business_rule_delegation` 對照。
