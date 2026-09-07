# 程式庫啟動器

此資料夾是主程式專屬啟動模組，不依賴每次重新打包 Electron。

- `scripts/start.ps1`：自動檢查依賴、只在 UI 來源更新時執行增量 build，並以正式來源模式啟動。
- `scripts/install.ps1`：以 MSVC `cl` 編譯輕量 Launcher EXE（原生 C++），安裝到 `%LOCALAPPDATA%\GPTBridgeLauncher`，再建立桌面 `程式庫.exe` 硬連結。
- `src/GPTBridgeLauncher.cpp`：桌面 EXE 的原生 Win32 輕量入口，只負責啟動專屬 PowerShell 模組（CREATE_NO_WINDOW + SW_HIDE）。
- `logs/launcher.log`：啟動與自動修復紀錄。
- `state/`：依賴與增量建置狀態。

安裝或修復桌面入口：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File launcher\scripts\install.ps1
```

直接啟動：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File launcher\scripts\start.ps1
```
