# AI投資管家 1.0

Project ID: `ai-assistant`

AI投資管家管理本機投資資料、參數與分析結果；自身不連外網、不直接呼叫外部 AI，也不執行舊式本機風險模型。所有本地模型均由本地模型平台（`local-model-platform`）處理；大部分投資業務使用 `ibm/granite4.2:30b-q4_K_M`，配息、股價、淨值及其他需要搜尋的即時資訊則由平台執行星澄原生模型（`star-main-native-model`）提供。星澄位於治理規則之下的最高權限層級，但沒有固定職責；權限僅能由治理規則明確啟用。

介面固定為「持股、星澄、星澄帳務、系統」四個工作區；低頻模擬與進階維護不占用主要操作畫面。AI 投資分析與帳務決策均由星澄提供，最終統籌結果由 ChatGPT 回傳星澄後再顯示。

## 治理邊界

- 最高權限是治理規則；星澄是 AI 通道頂層協調者。
- AI投資管家只能連接 `xingcheng`（星澄），不能申請或直接使用 AI協作。
- 外部 AI 的結果只能先回傳星澄，不能直接寫入 AI投資管家。
- 投資資料庫與星澄、其他工具的資料庫強制隔離；通道只傳遞最小必要快照。
- 網路權限為停用，只允許治理驗證的 loopback IPC。
- 配息頻率可手動修改；除非星澄取得可驗證的新資料，否則不得覆寫手動值。
- 只產生分析、警示與模擬草案，不會自動下單。

## 程式架構

- `investment_watch.py`：命令路由、生命週期、狀態與診斷。
- `investment_portfolio_service.py`：持股、帳本、同步與配息資料。
- `investment_operations_service.py`：壓力測試、回測、配置、排程與備份。
- `investment_accounting_service.py`：把最小對帳快照交給星澄，驗證其自主帳務決策後才由投資管家落盤。
- `investment_import_service.py`：Excel 掃描、欄位映射與匯入。
- `investment_star_service.py`：唯一的星澄 AI 通道邊界。
- `investment_mobile_bridge.py`：已分離手機工具的最小橋接介面。
- `investment-mobile/`：手機介面與連線程式的共用原始碼；手機版仍以 `investment-mobile` 獨立工具 ID 啟停，但設定、資料與投資業務只由 AI 投資管家的共用 repository 保存，兩者共用 `ai-investment-manager-v1` 業務權限。
- 手機版快取集中於 `ai-assistant/runtime/cache/companions/investment-mobile`；備份只由全域清理寫入 `global-cleaner/data/business/backups/ai-assistant`，不在手機介面建立快取或備份根目錄。
- `ExcelMappingEditor.tsx`、`HoldingEditor.tsx`：獨立表單元件；持股編輯採固定視窗，不受頁面捲動位置影響。
- `StarAccountingPanel.tsx`：星澄帳務的精簡專屬工作區。

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
