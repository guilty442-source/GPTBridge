# Global Update Coordinator Governance

## Rule ID

`G-HMR-GLOBAL-001`

## Purpose

全域更新不能只依賴前端 HMR。系統必須能分類變更，並決定使用介面熱更新、資料重新載入、後端重啟或應用程式重啟。

## Requirements

1. Backend owns change classification in `src-core/settings/global_update_coordinator.py`.
2. Settings health refresh returns `global_update_plan`.
3. Renderer owns update application in `src-ui/renderer/shared/services/globalUpdateCoordinator.ts`.
4. Electron exposes `app:restart-backend` for managed backend processes.
5. Update UI must expose a visible `套用全域更新` action.

## Enforcement

`npm run governance:check` runs `G-HMR-GLOBAL-001` and blocks drift.
