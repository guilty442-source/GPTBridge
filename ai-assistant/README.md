# AI投資管家 1.0

Project ID: `ai-assistant`

AI投資管家管理本機投資資料、參數與分析結果；自身不連外網、不直接呼叫外部 AI，也不執行舊式本機風險模型。所有本地模型均由本地模型平台（`local-model-platform`）處理；大部分投資業務使用 `ibm/granite4.2:30b-q4_K_M`，配息、股價、淨值及其他需要搜尋的即時資訊則由內建瀏覽器（`embedded-browser`）執行網路搜尋。AI投資管家位於治理規則之下，依治理法典行使工具層級服務擁有權。

介面固定為「持股、瀏覽器、帳務、系統」四個工作區；低頻模擬與進階維護不占用主要操作畫面。AI 投資分析與帳務決策均由AI投資管家提供，外部協作經 ai-collaboration 工具接入。

## 治理邊界

- 最高權限是治理規則；平台層級協調由星澄提供，工具層級服務由AI投資管家於治理法典之下行使。
- AI投資管家透過內建瀏覽器執行網路搜尋，透過 ai-collaboration 工具接入外部 AI。
- 外部 AI 的結果經 ai-collaboration 工具回傳AI投資管家，不能直接寫入投資資料庫。
- 投資資料庫與其他工具的資料庫強制隔離；通道只傳遞最小必要快照。
- 網路權限為內建瀏覽器視窗，工具核心不直接存取外部網路。
- 配息頻率可手動修改；除非內建瀏覽器取得可驗證的新資料，否則不得覆寫手動值。
- 只產生分析、警示與模擬草案，不會自動下單。

## 程式架構

- `investment_watch.py`：命令路由、生命週期、狀態與診斷。
- `investment_portfolio_service.py`：持股、帳本、同步與配息資料。
- `investment_operations_service.py`：壓力測試、回測、配置、排程與備份。
- `investment_accounting_service.py`：把最小對帳快照交給AI投資管家帳務，驗證其自主帳務決策後才落盤。
- `investment_import_service.py`：Excel 掃描、欄位映射與匯入。
- `investment_star_service.py`：唯一的 AI 通道邊界。
- `investment_mobile_bridge.py`：已分離手機工具的最小橋接介面。
- `investment-mobile/`：手機介面與連線程式的共用原始碼；手機版仍以 `investment-mobile` 獨立工具 ID 啟停，但設定、資料與投資業務只由 AI 投資管家的共用 repository 保存，兩者共用 `ai-investment-manager-v1` 業務權限。
- 手機版快取集中於 `ai-assistant/runtime/cache/companions/investment-mobile`；備份只由全域清理寫入 `global-cleaner/data/business/backups/ai-assistant`，不在手機介面建立快取或備份根目錄。
- `ExcelMappingEditor.tsx`、`HoldingEditor.tsx`：獨立表單元件；持股編輯採固定視窗，不受頁面捲動位置影響。
- `StarAccountingPanel.tsx`：帳務的精簡專屬工作區。

## 啟動與修復

啟動以 `dist/ai-assistant.exe` 為主，治理來源 UI 為備援；不硬性要求 EXE 才能啟動。啟動錯誤會交由工具自己的自動修復處理，主系統也能在收到明確封裝指令後重新建置 EXE。

目前所有程式碼與顯示版本統一為 1.0（manifest 儲存格式為 `1.0.0`）。版本或來源摘要不一致時，既有 EXE 視為過期，不得冒充目前來源。

## 測試

所有測試必須由全域清理的測試執行器啟動：

```powershell
python global-cleaner\src\test_runner.py -- python -m pytest -q ai-assistant\tests
python global-cleaner\src\test_runner.py -- npm.cmd --prefix main-system run type-check
python global-cleaner\src\test_runner.py -- npm.cmd --prefix main-system run smoke:ai-assistant-ui
```

測試產生物一律放入各工具或共用層的暫存區，測試完成後由全域清理刪除檔案並保留暫存資料夾。

所有投資分析均為輔助資訊，不構成投資建議。
