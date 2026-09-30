# GPTBridge

GPTBridge 是以 Windows 11 為目標的桌面應用程式啟動器。主系統只負責產品介面、經治理授權的工具探索與生命週期請求，以及 IPC 連線；不保存或實作獨立工具的業務能力。

獨立工具是 `E:\GPTBridge` 的直屬資料夾，主系統動態掃描 `<tool-id>/manifest.json`。每個工具擁有自己的程式碼、設定、暫存區與業務資料庫，支援原始碼和 EXE 兩種啟動方式。

## 環境需求

- Windows 11
- .NET 10 SDK（bootstrap/launcher 建置）
- MSVC（Visual Studio 18）C++ 工具鏈（原生核心與外層 EXE）
- Rust 工具鏈（src-tauri 桌面殼）
- Google Chrome（僅由需要瀏覽器能力的獨立工具使用）

Python 已全面退役（B166/B167/B38）：本專案不存在任何 Python 原始碼、
直譯器、虛擬環境、套件管理器或回退路徑，不得新增或呼叫。

## 安裝與啟動

```powershell
# 原生自辦安裝（bootstrap → desktop EXE）
launcher\bin\GPTBridge.Bootstrap.exe --install-desktop

# 直接啟動（準備 + 啟動 Tauri host）
launcher\bin\GPTBridge.Bootstrap.exe
```

受管 loopback IPC 使用 `127.0.0.1`（端口由受管通道指派）。

獨立工具視窗採用前景生命週期：使用者關閉視窗時，主系統會強制結束該工具完整程序樹並確認不再背景執行。此規則不套用於治理核准且明確宣告為無介面常駐服務的工具。

主系統每 24 小時經治理授權與系統通道請求全域清理。全域清理依序執行低風險清理與自動備份；主系統及每個獨立工具各自最多保留 1 份，且僅在新備份驗證成功後汰換同一擁有者的舊份。全域清理本身沒有排程器。

自動修復與獨立工具封裝器只有一個實作，均由系統救援集中管理，並以工具 ID 分隔修復資料庫。各工具不得直接執行自己的修復程式；需要備份資料時，系統救援只能透過治理系統通道向全域清理申請限定路徑提取。

## 架構

```text
GPTBridgeLauncher.exe（桌面 Win32 輕量入口）
  -> GPTBridge.Bootstrap（C# 14 / .NET 10 啟動管線）
       -> gptbridge-shell（Rust/Tauri 桌面殼 + WebView）
       -> governed loopback IPC / command router
       -> independent-tool lifecycle broker
            -> standalone tool processes
```

主要目錄：

- `launcher`: C# `GPTBridge.Bootstrap` 啟動管線與原生桌面入口
- `src-tauri`: Rust/Tauri 桌面殼與原生核心 crate
- `src-ui`: Native JavaScript ESM renderer 來源
- `src-core`: 已退役 Python 樹的墓碑殘留（僅 registry/port 資料檔）
- `<tool-id>`: 數量可動態增減的直屬獨立工具
- `governance_rule`: 唯一的治理與權限權威
- `shared-layer`: 只承載受治理的系統通道與 AI 通道
- `native/test_suites`: 原生 C++ 測試套件（pytest 已退役）
- `runtime`: 執行期狀態；不納入版本控制

更完整的系統邊界請見 [ARCHITECTURE.md](ARCHITECTURE.md)。

## 品質檢查

```powershell
# 治理審計（倉庫根目錄執行；原生 audit engine，Python 審計車道已退役）
native\test_suites\bin\audit-engine.exe --manifest governance_rule\execution\audit\audit_checks_manifest.json --root E:\GPTBridge
# 原生測試套件（MSVC 建置 + 受管平行執行）
powershell -ExecutionPolicy Bypass -File native\test_suites\build.ps1
# renderer 建置（SWC→ESM→esbuild 原生鏈，由 bootstrap 驅動）
launcher\bin\GPTBridge.Bootstrap.exe --force-build
```

CI 會阻擋前端建置、治理與環境檢查失敗。

## 執行期資料

下列目錄屬於本機狀態或產物，已由 `.gitignore` 排除：`runtime`、瀏覽器設定檔、`dist-ui`、`node_modules` 與 `.venv`。容量顯示會計入它們，因此開發環境容量不等於主系統原始碼容量。
