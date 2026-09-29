# 程式庫啟動器

此資料夾是主程式專屬啟動模組，不依賴每次重新打包 Electron。

`bootstrap-entry` 已依架構 registry 登錄為 `migrate-csharp`（A341/A610）：
啟動管線由 `src/GPTBridge.Bootstrap`（C# 14 / .NET 10）擁有。
B167/B38：Python 車道已全數退役——`scripts/start.py`、
`scripts/install.py`、`scripts/migration_autostart.py` 不再存在，
桌面安裝與 renderer 建置均由 bootstrap 原生自辦。

## 檔案結構

- `src/GPTBridge.Bootstrap/`：啟動管線主體（C#）。
  - 環境綁定（`GPTBRIDGE_RUNTIME_CONTEXT`、使用者環境變數）、
    單例 mutex（`Local\GPTBridgeLauncher`）、migration autostart
    狀態回報、桌面 EXE 新鮮度刷新、原生 renderer 建置、
    Tauri host 啟動。
  - `Program.RendererBuild.cs`：swc+esbuild renderer 建置驅動
    （原 `renderer_build.py` 的 C# 移植）。
  - `Program.Install.cs`：桌面安裝器（原 `install.py` 的 C# 移植）
    —— `dotnet publish` bootstrap、編譯外層 EXE（MSVC 優先、
    csc 降級）、寫入 `root.txt` 與 stamp、建立桌面硬連結。
  - 建置產出：`bin/GPTBridge.Bootstrap.exe`（`dotnet publish`）。
- `src/GPTBridgeLauncher.cpp`：桌面 EXE 原生 Win32 輕量入口
  （官方主要建置；MSVC）。只讀 `root.txt` 並隱藏啟動 bootstrap
  入口；找不到 bootstrap 時回報安裝缺失（無 Python fallback）。
- `src/GPTBridgeLauncher.cs`：同一契約的 csc.exe 降級建置
  （無 MSVC 時由原生安裝器使用）。
- `scripts/`：已退役的歷史腳本位址（`*.py` 已刪除；
  `install.ps1`/`start.ps1` 為 A349 封存說明檔，不可執行）。
- `logs/launcher.log`：啟動與自動修復紀錄（安裝側）。
- `state/`：依賴與增量建置狀態（`startup-journal.jsonl`、
  `ui-build.stamp`）。

## 安裝或修復桌面入口

```powershell
launcher\bin\GPTBridge.Bootstrap.exe --install-desktop
```

原生自辦安裝：`dotnet publish` 本 bootstrap → `launcher\bin/`、
MSVC（vswhere → vcvars64 → cl）編譯 `GPTBridgeLauncher.cpp` 或
csc 降級 `GPTBridgeLauncher.cs` → `%LOCALAPPDATA%\GPTBridgeLauncher\bin\專案程式庫.exe`、
寫入 `config\root.txt` 與 `state\launcher-stamp.json`、
桌面硬連結 `專案程式庫.exe`。

## 直接啟動

```powershell
launcher\bin\GPTBridge.Bootstrap.exe
```

參數：`--prepare-only`（只準備不啟動）、`--force-build`（強制重建 UI）、
`--project-root <path>`、`--install-desktop`（原生自辦安裝後離開）。

## 更新流程

- `GPTBridgeLauncher.cpp/.cs` 或 `GPTBridge.Bootstrap` 來源變更時，
  fingerprint 比對失敗 → 啟動時自動背景派生
  `GPTBridge.Bootstrap.exe --install-desktop` 自我重裝
  （`refresh.lock` 單飛、900 秒過期），安裝完成後下次啟動生效。
- bootstrap 常駐邏輯變更即時生效，不需重建外層 EXE。

## Migration autostart

受治理 `MigrationRunner`（原 `launcher/scripts/migration_autostart.py`）
隨 Python 車道退役；啟動器保留 fail-open 契約回報
`MIGRATION_RUNNER_RETIRED`，待原生 owner 指定後重新接線。
