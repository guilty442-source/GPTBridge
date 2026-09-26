# 程式庫啟動器

此資料夾是主程式專屬啟動模組，不依賴每次重新打包 Electron。

`bootstrap-entry` 已依架構 registry 登錄為 `migrate-csharp`（A341/A610）：
啟動管線由 `src/GPTBridge.Bootstrap`（C# 14 / .NET 10）擁有，
`scripts/start.py` 與 `scripts/migration_autostart.py` 為保留的
受治理 Python 模組（委派執行，不複製其治理內部邏輯）。

## 檔案結構

- `src/GPTBridge.Bootstrap/`：啟動管線主體（C#）。
  - 環境綁定（`GPTBRIDGE_RUNTIME_CONTEXT`、使用者環境變數）、
    單例 mutex（`Local\GPTBridgeLauncher`）、migration autostart 委派、
    桌面 EXE 新鮮度刷新、Node/Python/UI 建置檢查、Electron 啟動。
  - 建置產出：`bin/GPTBridge.Bootstrap.exe`（`dotnet publish`）。
- `src/GPTBridgeLauncher.cpp`：桌面 EXE 原生 Win32 輕量入口
  （官方主要建置；MSVC）。只讀 `root.txt` 並隱藏啟動 bootstrap 入口，
  找不到時退回 `scripts/start.py` + venv `pythonw.exe`。
- `src/GPTBridgeLauncher.cs`：同一契約的 csc.exe 降級建置
  （無 MSVC 時由 install.py 使用）。
- `scripts/install.py`：安裝器 — 編譯桌面 EXE（MSVC 優先、csc 降級）、
  發布 `GPTBridge.Bootstrap.exe`、寫入 `root.txt` 與 stamp、
  建立桌面硬連結、執行 `--prepare-only`。
- `scripts/start.py`：保留的 Python 啟動腳本（遷移期降級路徑）。
- `scripts/migration_autostart.py`：受治理 MigrationRunner 委派
  （shared-layer forward migration 報告/可選套用）。
- `logs/launcher.log`：啟動與自動修復紀錄（安裝側）。
- `state/`：依賴與增量建置狀態（`startup-journal.jsonl`、
  `ui-build.stamp`、`requirements.stamp`）。

## 安裝或修復桌面入口

```powershell
main-system\.venv\Scripts\python.exe launcher\scripts\install.py
```

## 直接啟動

```powershell
launcher\bin\GPTBridge.Bootstrap.exe
# 降級路徑（C# bootstrap 未發布時）：
main-system\.venv\Scripts\pythonw.exe launcher\scripts\start.py
```

參數：`--prepare-only`（只準備不啟動）、`--force-build`（強制重建 UI）、
`--project-root <path>`。

## 更新流程

- `GPTBridgeLauncher.cpp/.cs` 或 `GPTBridge.Bootstrap` 來源變更時，
  fingerprint 比對失敗 → 啟動時自動背景執行 install.py 重建
  （`refresh.lock` 單飛、900 秒過期），安裝完成後下次啟動生效。
- `start.py` / `migration_autostart.py` 變更即時生效，不需重建 EXE。
