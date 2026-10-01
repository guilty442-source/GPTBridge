# GPTBridge 全專案程式語言架構圖

本架構圖僅為 PostgreSQL Codex 的同步投影，不是法典鏡像或權威。法典檔案只保留五份唯讀中文鏡像；其他 SQL、資料庫、JSON、快取、匯出或備份形式的 Codex 鏡像一律禁止。

正式核心語言池含 C、C++23、C#、Rust、Go、F#；桌面 UI 可在 Tauri、Wails 與 Qt 中依應用自適化選定唯一殼層，並可使用受管 Bun 執行 JavaScript ESM。所有許可語言都可依能力需求使用，但每項能力仍須選定唯一現行擁有者並遵守型別邊界、權限與資源上限；語言選擇不得新增權威、重複實作或繞過治理。Python 維持全面禁止。

本文件是現行語言責任的非權威同步投影；正式責任以最新 PostgreSQL Codex 為準。

星澄採更嚴格的專屬子集合：只准 C、C++23、C#、F#、Rust，且其模型、訓練、推論、學習、評估與內部工具不得使用 Go、JavaScript／Bun、其他語言、外部模型、雲端服務、外部執行引擎或第三方推論／訓練框架。全專案的一般語言許可不會自動擴張星澄的語言池。

星澄完整原生領域不適用通用模組、檔案、類別、函式、宣告及公開入口的行數或數量上限；品質、安全、資源、介面、測試、審計、發布及可維護性邊界仍全部適用。

## 語言責任

| 技術 | 正式責任 | 禁止事項 |
| --- | --- | --- |
| C | 決定性 runtime、權限熱路徑、穩定 C ABI、原生測試 | UI、業務規則、資料權威 |
| C++23 | 模型推論、原生 runtime、測試執行、Audit Engine、Resource Governor | 公開應用 API、UI、工作流與資料權威 |
| Rust 1.98.1 | UI 核心、狀態、生命週期、IPC、安全、OS 整合、RAG／CAG／DAG、向量與 CPU 關鍵工作 | 治理或權限決策、業務判斷 |
| C# 14／.NET 10 | 應用、API、已授權工作流、Windows 整合、唯一測試編排 | F# 業務語義與前端 DOM |
| F# 10 | 業務規則、驗證、轉換、狀態轉移、資料分析、ML 與高正確性計算 | 治理、權限、UI、流程編排 |
| Go 1.27.1 | 高併發網路、搜尋、檔案與批次 I/O | 治理、權限及業務判斷 |
| Bun + JavaScript ESM | Tauri UI 呈現、互動、設定、表格、表單與工具面板 | 核心能力、資料權威、直接資料庫、原生 ABI、權限或業務決策 |
| SQL／PostgreSQL 18.6 | 集合式資料操作、完整性、RLS 與正式 schema | 工作流、權限來源及業務決策 |
| Python／NumPy／JAX | 已全面退役並立即生效：零角色、零常駐、零相依、零產物、零回退 | 任何殘留使用（fail-closed 拒絕） |

套件下載：套件管理的下載來源僅限 Visual Studio Installer 與 Winget 兩個通道。語言套件管理器不得自行連網下載套件；Bun 僅能執行受管 JavaScript ESM 並使用受管相依根內已 vendored／已登錄的產物；兩通道皆無法提供者須先取得明確許可。Python 全面禁止且無執行例外：不得存在現行 Python 原始碼、直譯器、虛擬環境、套件管理器、套件、相依、建置、測試、審計、訓練、推論、腳本、工具、常駐程序或回退路徑；不得下載、安裝、重新安裝、修復、還原、重建或補裝 Python、pip、NumPy、JAX 及其他 Python 套件。歷史文字只能作不可執行的歷史紀錄，不得成為現行依據。

Python 原責任由既有原生擁有者接手：C 負責決定性規則與穩定 ABI；C++23 負責模型訓練、推論、原生測試與審計熱路徑；Rust 負責程序、生命週期、安全、RAG、向量與 UI 核心；C# 負責應用、API、工作流及唯一測試編排；F# 負責業務規則、驗證、訓練評估、科學與高正確性分析；Go 負責檔案、網路及批次併發；PostgreSQL 負責集合式資料處理。移交不得保留 Python 代理、橋接、參考實作或回退。

核心程式碼以 C、C++23、C#、Rust、Go、F# 撰寫；核准桌面 UI 可由受管 Bun 執行已登錄且完整性驗證通過的 JavaScript ESM。Bun 是唯一 UI script-engine 例外，只能存在於選定的 Tauri 或 Wails UI 邊界，不得執行動態下載、`eval`、未登錄動態載入、套件安裝、程序派生或後端工作。其他直譯器、script engine、執行期原始碼編譯器與建置工具不得進入 production；C# 與 F# 正式發布採 NativeAOT 或等價預先編譯形式。

撰寫原則：所有新增或修改的程式碼一律以已登錄的原生擁有語言撰寫（C／C++23／Rust／Go／C#／F#，以及 UI 限定的 JavaScript ESM），並以高效方式實作：編譯形式、批次、零複製、容量預留、單趟處理、受限並行與期限取消；大型結構以型別化指標、參照、借用視圖或句柄傳遞，禁止不必要的整體值複製。熱迴圈優先採編譯器最佳化；實測有益時才使用迴圈展開與安全遞減計數，且不得造成溢位、錯誤終止或程式膨脹。語言支援且可證明無別名時善用 `restrict` 或等價契約；經量測的小型熱函式可使用 `inline`，但不得強制膨脹程式或改變語意。建置須善用已登錄、可重現且經量測的編譯器最佳化參數、連結時最佳化與目標架構設定；禁止未經證明即啟用會放寬數值、安全或相容語意的旗標。效能敏感路徑須有量測（p50／p95／p99、CPU、RAM、VRAM、queue depth）證據；直譯／腳本與已退役 runtime 不得作為撰寫或回退目標。

WebAssembly：已登錄的執行格式（UI／WebView 與受治理沙箱；Tauri 桌面殼層保留）。僅限已登錄之原生擁有語言編譯產生（Rust／C++ 優先）；wasm 不是來源語言、不得手寫，也不得自網路取得未登錄的 wasm 產物；套件下載、撰寫與沙箱契約規則一體適用。

## 執行拓撲

```mermaid
flowchart TB
  SHELL[Tauri, Wails or Qt Desktop UI] --> BUN[Bun + JavaScript ESM UI]
  BUN --> RUSTUI[Rust UI Core]
  RUSTUI --> API[Typed UI Contract]
  API --> CS[C# Application and Workflow]
  CS --> FS[F# Business and Validation]
  CS --> GO[Go Network File and Batch Services]
  CS --> RUST[Rust Runtime, UI Core, RAG and Vector]
  CS --> CABI[C Stable ABI]
  RUST --> CABI
  CABI --> CPP[C++23 Native Core]
  CS --> SQL[(PostgreSQL SQL)]
  RUST --> WASM[WebAssembly UI and Governed Sandbox]
  CPP --> WASM
```

跨語言邊界只能使用具內部時間戳記世代、具 owner、具型別、具期限與取消語義的正式契約。契約名稱不攜帶版本號，條文不顯示機器識別。Adapter 只能轉換型別、錯誤、生命週期與傳輸，不能取得目標能力的語義或權威。

## UI Stack

```mermaid
flowchart TB
  RUST[Rust Application Core] --> TAURI[Tauri Desktop Shell]
  GO[Go Application Core] --> WAILS[Wails Desktop Shell]
  CPP[C++23 Application Core] --> QT[Qt Desktop Shell and Native Views]
  TAURI --> BUN[Bun + JavaScript ESM UI]
  WAILS --> BUN
  QT --> RUSTUI
  BUN --> RUSTUI[Rust UI State, Lifecycle, IPC and Security]
  RUST --> GPUI[GPUI High-performance Views]
  RUST --> SLINT[Slint Native Declarative Views]
  RUST --> EGUI[egui Diagnostics]
```

Tauri、Wails 與 Qt 是核准桌面 UI 殼層；每個應用只能依其 Rust、Go 或 C++23 擁有邊界選用其中一個，不得多重宿主。Bun 是 Tauri／Wails 內唯一核准的 UI script-engine 例外，只能執行已登錄且完整性驗證通過的 JavaScript ESM，禁止動態下載、`eval`、未登錄動態載入、套件安裝、程序派生與後端工作；Qt、GPUI、Slint 及 egui 維持原生執行。Electron、TypeScript、Node.js、React 與 Python 禁用；語言選擇不得直接連線權威資料庫、取得未授權權責或形成重複實作。

## Python／NumPy／JAX 退役

```mermaid
flowchart LR
  RETIRED[Python / NumPy / JAX] --> REMOVED[Retired: no role, no residency, no dependency, no artifact, no fallback]
  REMOVED --> DENY[Any residual use is denied fail-closed]
```

Python、NumPy 與 JAX 已全面退役並立即生效。不得承擔任何角色、常駐程序、相依套件、模型產物或回退路徑；任何殘留引用一律 fail-closed 拒絕，替代能力須經另行登錄的修訂案。

模型訓練能力不隨退役技術消失：C++23 是星澄內部原生訓練與模型產物唯一執行擁有者，F# 是訓練評估與高正確性分析擁有者；原獨立 MODEL_TRAINING 模組維持退役。

## 測試與審計

```mermaid
flowchart LR
  CS[C# TestSuiteOrchestrator] --> RUNNERS[Native Language Runners]
  RUNNERS --> TEST[Typed Test Result]
  TEST --> AUDIT[C++23 Audit Engine]
  AUDIT --> EVIDENCE[Typed Audit Result]
```

所有測試由被測能力所屬語言的原生 runner 執行；C# 只負責唯一編排。審計由 C++23 執行。不支援項目標記 `BLOCKED` 並拒絕發布，不得委派給 Python 或靜默通過。

## 效能與格式

```mermaid
flowchart LR
  INPUT[Workload and Contract] --> OWNER[Language Owner]
  OWNER --> DESIGN[Algorithm, Data Structure and Format]
  DESIGN --> BOUND[Bounded Concurrency and Resources]
  BOUND --> MEASURE[p50 p95 p99, Throughput, CPU, RAM, VRAM, IO]
  MEASURE --> ACCEPT[Correct, Secure, Fast and Low-cost]
```

所有程式碼與檔案格式須減少不必要的配置、複製、序列化、鎖競爭、程序／語言跳轉、系統呼叫及資料庫／IPC／網路往返。高頻資料採量測證明的具型別格式；大型資料避免整檔載入；所有 queue、thread、goroutine、task、process、cache 與 batch 必須有明確上限。

## 各語言高效撰寫模式

- **C++（零成本抽象與記憶體控制）**：`std::move` 移轉所有權；唯讀參數用 `std::string_view`；容器先 `reserve()`。
- **C#（與 GC 共存）**：`ValueTask`／`async`／`await` 減少配置；`Span<T>`／`ReadOnlySpan<T>` 切片不新配置；小型資料用 `struct`。
- **F#（尾端遞迴與管線）**：尾端遞迴化為迴圈；大數據用 `seq` 惰性求值；小型 DU 加 `[<Struct>]`。
- **Go**：能用值不用指標；`make` 帶 capacity；大字串用 `strings.Builder`（迴圈禁 `+`）；高頻物件用 `sync.Pool`；不盲目開 Goroutine。
- **Rust**：多用 `&[T]`／`&str`，不亂 `clone()`；迭代器取代索引迴圈；集合 `with_capacity`。

編譯器優化：production 一律使用擁有語言的 release 優化組態——C／C++ 為 `/O2` 或 `-O3`（必要時 LTO）；Rust 為 `cargo build --release`（必要時 `lto=true`／`codegen-units=1`）；C#／F# 為 Release＋optimize（Tiered PGO 依量測）；Go 為最佳化 release 建置＋`-trimpath`。未優化或 Debug 組態不得進入 production，實際組態記入 B79 工具鏈／ABI 證據。
