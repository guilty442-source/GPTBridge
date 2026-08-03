# GPTBridge

GPTBridge 是以 Windows 11 為目標的桌面應用程式啟動器。主系統只負責產品介面、經治理授權的工具探索與生命週期請求，以及 IPC 連線；不保存或實作獨立工具的業務能力。

獨立工具是 `E:\GPTBridge` 的直屬資料夾，主系統動態掃描 `<tool-id>/manifest.json`。每個工具擁有自己的程式碼、設定、暫存區與業務資料庫，支援原始碼和 EXE 兩種啟動方式。

## 環境需求

- Windows 11
- Node.js 20+
- Python 3.11
- Google Chrome（僅由需要瀏覽器能力的獨立工具使用）

## 安裝與啟動

```powershell
npm ci
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm start
```

Python IPC 使用 `127.0.0.1:8765`。

獨立工具視窗採用前景生命週期：使用者關閉視窗時，主系統會強制結束該工具完整程序樹並確認不再背景執行。此規則不套用於治理核准且明確宣告為無介面常駐服務的工具。

主系統每 24 小時經治理授權與系統通道請求全域清理。全域清理依序執行低風險清理與自動備份；主系統及每個獨立工具各自最多保留 1 份，且僅在新備份驗證成功後汰換同一擁有者的舊份。全域清理本身沒有排程器。

自動修復與獨立工具封裝器只有一個實作，均由系統救援集中管理，並以工具 ID 分隔修復資料庫。各工具不得直接執行自己的修復程式；需要備份資料時，系統救援只能透過治理系統通道向全域清理申請限定路徑提取。

## 架構

```text
Electron main process
  -> React launcher renderer
  -> Python backend manager
       -> WebSocket command router
       -> governance request adapter
       -> independent-tool lifecycle broker
            -> standalone tool processes
```

主要目錄：

- `src-ui`: Electron main process、preload 與 React renderer
- `src-core`: Python 啟動、IPC 與工具生命週期請求
- `<tool-id>`: 數量可動態增減的直屬獨立工具
- `governance_rule`: 唯一的治理與權限權威
- `shared-layer`: 只承載受治理的系統通道與 AI 通道
- `system-rescue`: 集中修復、封裝器、封裝模板與修復資料
- `tests`: Python 單元與整合測試
- `runtime`: 執行期狀態；不納入版本控制

更完整的系統邊界請見 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 品質檢查

```powershell
npm run type-check
npm run governance:check
npm run check:circular
npm run governance:audit
npm run doctor -- --json
.\.venv\Scripts\python.exe -m pytest -q
```

CI 會阻擋型別、治理、循環依賴、依賴安裝與 Python 測試失敗。

## 執行期資料

下列目錄屬於本機狀態或產物，已由 `.gitignore` 排除：`runtime`、瀏覽器設定檔、`dist-ui`、`node_modules` 與 `.venv`。容量顯示會計入它們，因此開發環境容量不等於主系統原始碼容量。
