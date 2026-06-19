# [Level 1] Module Governance

## 1. Module Splitting (G-004)
邏輯上獨立的功能必須拆分為獨立模組。禁止巨型單檔或 God Object。

## 2. Module Isolation (G-002)
模組之間必須嚴格解耦，禁止循環依賴。共用邏輯必須存放於 `src-ui/renderer/shared/`。

## 3. Dashboard Purity (G-015)
Dashboard 模組僅限於狀態展示、摘要與導航入口。禁止執行工具、服務控制、運行時變異或後端操作。

## 4. Root Directory Purity (G-017)
專案根目錄僅限存放配置、腳本、頂層入口及治理文件。禁止散落 `.tsx`、運行時或暫存檔案。

## 5. Canonical Placement (G-059)
每種檔案類型必須有其規範的存放位置。例如：運行時相關檔案應在 `runtime/`，服務相關檔案在 `services/`。

## 6. Complexity Budget
每個模組必須有其複雜度預算。超過預算時，強制進行拆分或治理審查。
## 7. Child Tool Isolation (G-115)
1. Child-tool implementation code must live under `release/child-tools/<tool-name>/`.
2. Mother-tool core (`src-core/**`) and Electron main (`src-ui/main/**`) must not hardcode child-tool runtime logic.
3. Renderer toolbox can keep only entry metadata and localized names.
4. Child-tool runtime behavior must be implemented and maintained inside the child-tool folder.
