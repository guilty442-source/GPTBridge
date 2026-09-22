# 審計 C/C++ 化 ≤30s 計畫（A537）

> 現況：`native/test_suites/build.ps1:1` C 測試套件 6.6s PASS（25/0/3），`python -m governance_rule.execution.audit` 17s PASS（偶發 58s 冷啟動），但 `pytest` 全量 415s 嚴重超標，pre-commit 稽核在負載下 30.3/61.7s 兩次逾時（藍圖 P0「測試與審計套件 ≤30s」）

## 目標

- 審計引擎以 **C／C++** 實作熱路徑（`governance_rule/execution/audit` 的 `check_architecture_registry`、`check_codex`、`check_provision` 等），Python 僅作編排與 fallback
- 提交閘門僅跑 **C 快速套件**（≤30s），全量 pytest 移出提交閘門（離峰／夜間），審計快取失效策略（法典變更即失效）

## 現有資產

- `native/test_suites/harness.hpp:1`（C++17，PASS/FAIL/BLOCKED JSON）
- `native/core/memory.h:1` + `native/include/gptbridge_native.h:1`（C ABI）
- `governance_rule/execution/audit/architecture_registry.json:1`（單一權威）

## 步驟

1. **C 審計引擎原型**（`native/audit/audit_engine.c`，純 C）：輸入 `architecture_registry.json` + `governance_codex.sqlite3` 路徑，輸出 `audit_result.json`（PASS/FAIL，hash 驗證）
2. **增量/快取**：法典 `content_sha` 未變即命中快取，否則全量重跑
3. **提交閘門**：`pre-commit` 僅調 `audit_engine.exe`（≤5s 預期），`pytest` 全量改 `nightly` 排程
4. **驗收**：`audit_engine.exe` 在 10 次連續提交（負載下）皆 ≤30s，`pytest --co -q` 全量仍 415s 但不阻提交

## 驗收

- `audit_engine.exe` 單次 ≤5s，10 次連續 ≤30s（p95），`git diff --cached --check` 仍 fail-closed
- 工作者測試硬上限 30s（訓練除外）— 超時即中止並直接修正，不得放行

## 依賴

- P1 執行模組外移（`native/core/runtime_core` 為參考）
- 藍圖 §10.65 E1-E4 與 5 核心同置
