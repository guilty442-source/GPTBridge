# GPTBridge 全專案完整架構圖

程式語言分工由 C、C++23、C#、Rust、Go、F# 六語言池在既有責任與契約內自適化配置。既有分工只作原則參考與初始偏好，不構成固定語言綁定；每項能力同時只能有一個主要語言擁有者，並以正確性、安全、效能、資源與維護證據決定。調整只能在六種語言內進行，不得改變權威、權限、業務語意、引入第七種語言或建立重複執行路徑。Python 與直譯式執行全面禁止，正式環境只執行已驗證的預編譯原生產物。

本文件是現行 PostgreSQL Codex 與架構登記的非權威同步投影。衝突時以最新已發布 Codex 為準；業務規則與實作細節由各 owner-local contract 管理。

## 一、權威與五核心

```mermaid
flowchart TB
  USER[使用者] --> UI[主系統 Tauri UI]
  UI --> INFO[Information Channel]
  CODEX[(PostgreSQL Codex<br/>唯一治理權威)] --> DEC[Decision Core]
  CODEX --> PERM[Permission Core]
  CODEX --> RUN[Runtime Core]
  CODEX --> AUTO[Automation Core]
  CODEX --> XA[星澄助理]
  INFO --> DEC
  DEC --> PERM
  PERM --> RUN
  RUN --> EXEC[Registered Modules]
  AUTO --> EXEC
  EXEC --> RECEIPT[Typed Receipt and Audit]
  RECEIPT --> XA
  XA --> UI
  XC[星澄原生模型] -.受治理通道.-> INFO
```

| 核心／機構 | 唯一責任 | 明確禁止 |
| --- | --- | --- |
| Decision Core | 政策、優先順序、變更與結果接受 | 直接執行模組工作 |
| Permission Core | 身分、範圍、權限、目錄與安全執法 | 產生領域事實或代替決策 |
| Runtime Core | 啟動、程序生命週期與受治理執行 | 自行授權或修改政策 |
| Automation Core | 已授權工作流排程、同步、更新與維護 | 自行產生權限或接受結果 |
| 星澄助理 | 全域稽查、通知及使用者單項許可介面 | 代替使用者選擇或保存星澄資料 |
| 星澄 | 原生模型、自我學習、自動編程與修復能力 | 成為獨立工具、法典或權限權威 |

次主權層不存在。模組只有單一 owner、單一主要責任及以內部時間戳記管理世代的契約，不具獨立權威。契約名稱不含版本號，架構文件不顯示機器識別。

## 二、啟動與生命週期

```mermaid
flowchart LR
  EXE[MAIN_SYSTEM_DESKTOP_EXE] --> UI[Visible Main UI]
  UI --> START[SYS_GPTBRIDGE_START]
  START --> INFO[Information Layer]
  INFO --> READY[Core READY within 20 seconds]
  READY --> LAZY[Noncritical capabilities lazy start]
  CLOSE[Intentional Main UI Close] --> STOP[Typed Shutdown]
  STOP --> VERIFY[Matching Backend Verified Exit]
  TOOLS[Independent Tools] -.不受主系統關閉牽連.-> RUNNING[Remain Running]
```

主系統啟動必須在 20 秒內完成。七個獨立工具各自啟動與關閉須在 5 秒內完成；工具視窗關閉只停止該工具自身完整程序樹。主系統關閉不得關閉獨立工具。

## 三、七個獨立工具

```mermaid
flowchart TB
  ROOT[Standalone Tools]
  ROOT --> T1[ai-assistant<br/>投資管家]
  ROOT --> T2[ai-collaboration<br/>外部協作]
  ROOT --> T3[file-sorter<br/>自動化檔案管理]
  ROOT --> T4[investment-mobile<br/>投資管家手機版]
  ROOT --> T5[local-model<br/>本地模型]
  ROOT --> T6[vaultly<br/>影音下載自動化]
  ROOT --> T7[model-dialogue<br/>模型對話]
  T5 --> XC[星澄原生模型]
```

只有上述七項是獨立工具。星澄、星澄助理、搜尋服務、資源管制器、資料服務及修復／學習能力均不是獨立工具；已退役工具不得重新進入現行拓撲。

## 四、資料與 SQL

```mermaid
flowchart LR
  APP[Authorized Application Service] --> SEC[Permission Artifact]
  SEC --> PG[(PostgreSQL 18.6)]
  PG --> OFFICIAL[Canonical Structured Data]
  PG --> TRANSPORT[Shared Transport]
  PG --> AUDIT[Central Append-safe Audit]
  PG --> VECTOR[pgvector Canonical Vector Metadata]
  PG --> EVENT[Index Event]
  EVENT --> VD[vectord Native Service<br/>Rebuildable Rust Index]
  VD --> IDS[Candidate IDs]
  IDS --> PG
```

PostgreSQL 是唯一正式結構化資料、共享傳輸、中央審計、權限投影及向量中繼資料權威。已刪除的嵌入式資料庫與 Qdrant 沒有現行消費者、回退或權威角色。vectord 原生服務只保存可重建的衍生語意索引，不得保存唯一業務真相。

法典只固定原則、權威、權責、禁止事項、安全邊界、契約與資源上限。各擁有層可在這些原則內依即時量測自動調整演算法、資料結構、批次、併行、快取、查詢計畫、模型駐留與維護節奏；不得藉自適化改寫法典、擴張權限、改變業務語意或建立第二權威。

每個 PostgreSQL schema、table、view、function、trigger、index、sequence、type、policy、role、grant、extension 與 publication 都必須具有 canonical identity、owner、authority class、migration provenance、定義雜湊與生命週期。正式 schema 只能由不可變、排序、連續且可重播的 migration chain 建立；宣告、重播與 live introspection 不一致即 `SQL_SCHEMA_DRIFT` 並 fail-closed。

## 五、DAG／CAG／RAG

```mermaid
flowchart LR
  CALLER[Caller] --> INFO[Information Channel]
  INFO --> SCOPE[Permission and Scope]
  SCOPE --> APP[RagApplicationService]
  APP --> DAG[DAG Planner and Executor]
  DAG --> CAG[CAG Gate]
  CAG --> RAG[RAG Retrieval]
  RAG --> FUSE[Evidence Fusion]
  FUSE --> RERANK[Reranker]
  RERANK --> CONTEXT[Context Builder]
  CONTEXT --> LLM[Local LLM / 星澄]
  LLM --> CITE[Citation Validation]
  CITE --> RESULT[Result]
```

- DAG 是有向無環的工作流編排平面，不取代 Application Service。
- CAG 是有界、由內部時間戳記區分世代、範圍化且非權威的快取增強平面，不取代 RAG。
- RAG 是 canonical retrieval 平面，保留 Hybrid、Code、Memory、Agentic 四種子架構。
- PostgreSQL 保留來源、範圍、內部時間戳記世代、權限與發布狀態；vectord 原生服務只提供候選索引。

## 六、Git 全自動維護

```mermaid
flowchart LR
  WORK[Worker Change] --> COMMIT[Scoped Auto Commit]
  COMMIT --> QUEUE[Integration Queue]
  QUEUE --> PRE[Conflict and Governance Precheck]
  PRE --> MERGE[Main Integration]
  MERGE --> AUDIT[Native Audit and Evidence]
  AUDIT --> FF[Fast-forward Clean Worktrees]
  FF --> PUSH[Coordinator-only Push]
```

Git 只管理原始碼版本與開發歷史。每次提交限定明確路徑，禁止掃入其他工作者已暫存內容。只有同步協調器可推送；禁止 force-push、刪除 ref、無證據衝突覆寫與第二套 Git 編排器。可確定的生成檔衝突按已登錄策略重建；真實語義衝突 fail-closed。

## 七、程式語言與 UI

```mermaid
flowchart TB
  RUST[Rust 1.98.1<br/>UI Core, State, Lifecycle, IPC, Security, RAG and Vector]
  TAURI[Tauri<br/>Desktop Shell and WebView]
  JS[Native JavaScript ESM + JSDoc<br/>General UI]
  GPUI[GPUI<br/>Model Dialogue and Coding Workspace]
  EGUI[egui<br/>Diagnostics and Engineering Console]
  RUST --> TAURI --> JS
  RUST --> GPUI
  RUST --> EGUI
```

Electron、TypeScript、Node.js、React 不屬現行 UI 架構。C／C++／Rust 承載高頻原生工作，C++23 亦承載星澄內部原生訓練與正式推論，C# 承載應用與工作流，F# 承載業務規則、訓練評估與高正確性分析，Go 承載高併發網路／檔案／批次，Julia 承載科學計算。Python 全面禁止並立即生效：零角色、零原始碼、零直譯器、零虛擬環境、零套件、零相依、零建置、零測試、零審計、零訓練、零推論、零腳本、零工具、零常駐、零產物、零回退；禁止下載、安裝、重新安裝、修復、還原、重建或補裝 Python、pip、NumPy、JAX 及其他 Python 套件。歷史文字只能作不可執行的歷史紀錄。原獨立 MODEL_TRAINING 模組維持退役，訓練僅為星澄內部能力。套件下載僅限 Visual Studio Installer 與 Winget 兩個通道；語言套件管理器（Cargo、Go modules、NuGet、npm、pip、Bun 等）不得連網下載（禁止透過 Bun 下載套件），只能使用受管相依根內已 vendored／已登錄的產物；兩通道皆無法提供者須先取得明確許可。Node 全面禁止：不得下載、安裝、重新安裝、執行或常駐。所有新增或修改的程式碼一律以已登錄之原生擁有語言撰寫，並以高效方式（編譯形式、批次、零複製、容量預留、單趟處理、受限並行）實作，須有量測證據；直譯／腳本與已退役 runtime 不得作為撰寫或回退目標。WebAssembly 為已登錄執行格式（UI 與受治理沙箱），僅得由已登錄原生擁有語言編譯產生，不得手寫或自網路取得未登錄產物；Tauri 桌面殼層保留。TypeScript 與 Node 全面禁用並立即生效；JavaScript-ESM 少用，互動關鍵與高頻視圖以原生 GPUI／egui 為主。

Python 原責任依既有所有權直接收斂：C＝決定性規則與 ABI；C++23＝模型訓練、推論、原生測試及審計熱路徑；Rust＝程序、生命週期、安全、RAG、向量與 UI 核心；C#＝應用、API、工作流與唯一測試編排；F#＝業務規則、驗證、訓練評估與高正確性分析；Go＝檔案、網路及批次併發；Julia＝科學與大量數值運算；PostgreSQL＝集合式資料處理。禁止以 Python 橋接、代理、參考實作或回退保留舊責任。

正式程式碼語言限定為 C、C++23、C#、Rust、Go、F#。編譯器與連結器只屬受管建置工具；正式 runtime 不得啟動編譯器、直譯器、即時原始碼編譯器或腳本引擎，只執行已驗證的預編譯原生產物。C# 與 F# 發布使用 NativeAOT 或等價預先編譯形式，禁止依賴執行期動態編譯。

## 八、測試、審計與資源

```mermaid
flowchart LR
  SRC[Source Revision] --> CSHARP[C# TestSuiteOrchestrator]
  CSHARP --> NATIVE[C/C++/Rust/Go/.NET/JS/Julia Native Runners]
  NATIVE --> TEST[Typed Test Result]
  TEST --> CPP[C++23 Audit Engine]
  CPP --> AUDIT[Typed Audit Result]
  AUDIT --> GATE[Release Gate]
  GOV[C++23 Resource Governor] --> NATIVE
  GOV --> CPP
```

C# 是唯一測試編排器；C++23 Audit Engine 是正式審計引擎；Python 與 pytest 不承擔正式測試、審計、驗證、發布或推送責任。必要測試套件須在 30 秒內完成，各獨立審計流程不得超過 60 秒。C++23 Resource Governor 是唯一全專案資源管制器，具限於資源管制的系統管理員身分。

## 九、星澄資料邊界

```mermaid
flowchart LR
  XC[星澄] --> XDATA[(星澄專屬資料域)]
  XDATA --> TRAIN[Training and Weights]
  XDATA --> MEMORY[Identity, Personality and Memory]
  XDATA --> REPAIR[Repair Knowledge and Runtime Records]
  XDATA --> OUT[Minimum Typed Result or Opaque Reference]
  OUT --> INFO[Governed Information Channel]
  XA[星澄助理] -.不得保存星澄資料.-> INFO
```

星澄擁有或為星澄產生的資料只能保存在星澄專屬領域。其他核心、工具與星澄助理只能取得完成工作所需的最小型別化結果或不透明參照，不得保存、複製、快取、建立索引、拿來訓練或取得資料權威。

## 十、程式碼與格式

所有新增或修改的程式碼與檔案格式必須同時追求高效能、高速、低延遲與低資源消耗，且不得犧牲正確性、安全、權威邊界、可攜性、可審查性或可維護性。並行必須有界並具背壓、期限、取消與清理；大量操作優先批次；大型結構以型別化指標、參照、借用視圖或句柄傳遞，不得整體按值複製；大型資料優先參照、串流、分塊、映射或合適的零複製路徑。熱迴圈先由編譯器最佳化；只有代表性量測證明改善時，才採迴圈展開與不會溢位或破壞終止條件的遞減計數。語言支援且無別名關係可證明時採 `restrict` 或等價契約；小型熱函式可依量測採 `inline`，不得造成程式膨脹或語意改變。建置使用已登錄、可重現且經量測的編譯器最佳化參數、連結時最佳化與目標架構設定；任何會放寬數值、安全或相容語意的旗標必須先證明不改變正式契約。格式必須明定 owner、內部時間戳記世代、編碼、大小上限、未知欄位及相容政策。

## 十一、架構不變條件

1. 最新 PostgreSQL Codex 是唯一治理權威；中文法典與本文件均為同步投影。
2. 權限資料庫投影只執行 Permission Core 的決定，不自行創造權限。
3. 每個能力、資料與契約只有一個 canonical owner。
4. 快取、索引、遙測、備份與模型輸出永不因存在而取得權威。
5. 未登錄通道、無界資源、靜默降級、第二套編排器與第二套資料權威一律禁止。
6. 任何完整性、schema、權限、authority 或時間戳記世代衝突均 fail-closed。
