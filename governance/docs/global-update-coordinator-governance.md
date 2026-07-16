# Global Update Coordinator Governance

## Rule ID

`G-HMR-GLOBAL-001`

## Purpose

更新不能只依賴前端 HMR。v1.0 必須以檔案指紋找出受影響的獨立工具，先執行宣告式自動修正，再只重啟仍在運行且受影響的工具。

## Requirements

1. Backend owns change classification in `src-core/settings/global_update_coordinator.py`.
2. `src-core/settings/update_repository.py` persists fingerprints and repair results in the main-only database.
3. `src-core/core_system/hot_update_service.py` applies repair before restart.
4. Only affected running tools are restarted; unrelated tools remain online.
5. Runtime-contract or EXE payload changes require verified repackaging.

## Enforcement

`npm run governance:check` runs `G-HMR-GLOBAL-001` and blocks drift.
