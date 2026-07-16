# GPTBridge

GPTBridge 是以 Windows 11 為目標的桌面應用程式啟動器。Electron/React 負責桌面介面，Python 核心負責 WebSocket IPC、模式管理、治理規則、備份與平台工具服務。

目前內建六個獨立平台工具：Agent Coder、AI Assistant、AI Collaboration、File Sorter、Project Cleaner 與 Vaultly。各工具由 `platform_tools/<tool>/manifest.json` 描述，並以獨立 Python 入口與 EXE 發佈。

## 環境需求

- Windows 11
- Node.js 20+
- Python 3.11
- Microsoft Edge

## 安裝與啟動

```powershell
npm ci
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
npm start
```

開發模式：

```powershell
npm run dev
```

Electron renderer 使用 `127.0.0.1:5180`，Python IPC 使用 `127.0.0.1:8765`。

## 架構

```text
Electron main process
  -> React launcher renderer
  -> Python backend manager
       -> WebSocket command router
       -> safe/full mode manager
       -> governance and backup services
       -> platform tool service registry
            -> standalone tool processes
```

主要目錄：

- `src-ui`: Electron main process、preload 與 React renderer
- `src-core`: Python 啟動、IPC、模式、治理與共用服務
- `platform_tools`: 六個可獨立執行的平台工具
- `governance`、`src-governance`: 靜態治理檢查與規則實作
- `tests`: Python 單元與整合測試
- `runtime`: 執行期狀態；不納入版本控制

更完整的系統邊界請見 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 品質檢查

```powershell
npm run type-check
npm run governance:check
npm run check:circular
npm run audit:structure
npm run doctor -- --json
.\.venv\Scripts\python.exe -m pytest -q
```

CI 會阻擋型別、治理、循環依賴、依賴安裝與 Python 測試失敗。

## 執行期資料

下列目錄屬於本機狀態或產物，已由 `.gitignore` 排除：`runtime`、`backups`、`edge-profile`、`.GPTBridge_RuntimeSandbox`、`dist-ui`、`node_modules` 與 `.venv`。
