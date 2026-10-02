# 星澄／完整架構圖

> 規範性檢視檔。唯一權威仍為星澄法典、正式登錄表與權威資源；本檔不得創設規格、固定實作或取代權威來源。元件、路徑與邊界為現行實作的鏡像描述，若與現行權威衝突，以現行權威與正式登錄表為準。

## 行程拓撲

```mermaid
flowchart TB
    subgraph HOST["主系統 / 啟動層"]
        BE["gptbridge-backend (Rust Tauri)<br/>tools::start_tool / dispatch_command"]
        AUTO["GPTBridge.Automation (C#)<br/>automation-flows.json 驅動"]
        CH["GPTBridge.ChannelHost (C#)<br/>authenticated WS + PG LISTEN/NOTIFY"]
    end

    subgraph TOOL["xingcheng 工具行程 (xingcheng/xingcheng/dist/GPTBridge.ToolHost.App.exe)"]
        EXEC["XingchengModelServiceExecutor (C#)<br/>loopback HTTP /v1 + session token"]
        IPC["xingcheng/runtime/ipc/model-service.json<br/>star-model-service-descriptor/v1"]
    end

    subgraph NATIVE["C++ native 層"]
        MODELTOOL["xc_modeltool.exe<br/>91 modes + serve(JSONL stdio worker)"]
        ENGINE["xingcheng_engine.dll<br/>xc_engine_* C ABI / engine*.h"]
        TRAINER["xingcheng_trainer.exe<br/>star-native-train-job/v1"]
        CUDA["cuda_bridge.cpp / cuda_kernels.cpp<br/>Driver API (nvcuda.dll) + in-tree PTX kernels<br/>cuda_ptx_* · cuModuleLoadData JIT (opt-in)"]
    end

    subgraph RUST["Rust 資料面"]
        XSTORE["xstore.exe<br/>content-addressed store + XCN verify<br/>（物件/快照/內容雜湊/衍生索引；metadata 權威在 PG）"]
        XCORPUS["xcorpus.exe / xcorpus.dll<br/>corpus pipeline + xtok tokenizer ABI"]
    end

    subgraph CSHARP["C# 治理層 (xc-learning.exe)"]
        SL["SelfLearning.cs<br/>cycle + gates"]
        JE["JobExecutor.cs + NativeTools.cs<br/>supervised subprocess lane"]
        EV["Evaluation.cs / Lifecycle.cs<br/>eval gates + star-model-lifecycle/v1"]
        MAT["Maturation300M.cs<br/>ordered capability sequence"]
        RET["Retention.cs"]
    end

    subgraph QUEUE["xct-executor.exe (C#)"]
        FQ["file-queue: req/claimed/done/failed"]
    end

    subgraph EXT["外部依賴"]
        PG[("PostgreSQL<br/>gptbridge_xingcheng* schemas<br/>（正式結構化 metadata 權威）")]
        CONSUMERS["model-dialogue ToolHost / NativeModelClient<br/>(C# orchestrator-only consumers)"]
    end

    BE -->|CreateProcess + injected env| TOOL
    EXEC -->|post-bind write| IPC
    EXEC -->|first /v1/infer → spawn| MODELTOOL
    MODELTOOL --- ENGINE
    MODELTOOL -->|LoadLibrary xtok| XCORPUS
    MODELTOOL -->|extern C xcuda_*| CUDA
    CONSUMERS -->|read descriptor + POST /v1/infer| EXEC
    CONSUMERS -->|NativeLibrary.Load engine_c.h| ENGINE

    SL --> PG
    SL --> JE
    JE -->|tokenize via| MODELTOOL
    JE -->|--job/--report| TRAINER
    JE --> FQ
    TRAINER -->|XCB1 input| XCORPUS
    SL -->|snapshot pin| XSTORE
    EV -->|eval/capability modes| MODELTOOL
    SL -->|teacher-collect serve/infer<br/>native self-distillation| MODELTOOL

    AUTO -.->|self-learning flow| SL
    CH -.->|request_channel| TOOL
```

## 語言 lane 與權責

| Lane | 位置 | 權責 |
|---|---|---|
| C# 治理 | `src/backend/csharp/GPTBridge.XingchengLearning` → `xc-learning.exe` | 編排、政策閘門、DB、生命週期、稽核 |
| C++ native | `src/backend/cpp/` + `native_transformer/` | 模型核心、推論引擎、訓練器、模型工具 |
| Rust 資料面 | `src/backend/rust/xstore`、`xcorpus` | 不受信任輸入邊界：剖析、儲存、語料、tokenizer |
| C ABI | `xingcheng_engine_c.h`、`xtok_abi.h`、`xcuda_*` | 跨語言邊界，僅 ABI 不承載治理 |

## 二進位清單

| Binary | 語言 | 啟動方式 |
|---|---|---|
| `GPTBridge.ToolHost.App.exe` | C# | backend `CreateProcess` + injected env |
| `xc_modeltool.exe` | C++ | ToolHost 惰性 spawn（`serve --bundle`）或直接 CLI |
| `xingcheng_trainer.exe` | C++ | JobExecutor 受管子行程 / xct-executor |
| `xct-executor.exe` | C# | `serve` verb file-queue |
| `xc-learning.exe` | C# | CLI / AutomationCore flow |
| `xstore.exe` | Rust | 子行程（JSON-on-stdout） |
| `xcorpus.exe` / `.dll` | Rust | 子行程 / engine LoadLibrary |
| `xingcheng_engine*.dll` | C++ | NativeModelClient NativeLibrary.Load |

<!-- autogen:xingcheng-binaries -->
*autogen-scanner/v1 · 2492 files · main+devin+git+local-model+rag+ui*
| Binary | 語言 | 來源 |
|---|---|---|
| `xc-format.exe` | Rust | devin:xingcheng/src/backend/rust/xc-format/Cargo.toml |
| `xc-format.exe` | Rust | git:xingcheng/src/backend/rust/xc-format/Cargo.toml |
| `xc-format.exe` | Rust | local-model:xingcheng/src/backend/rust/xc-format/Cargo.toml |
| `xc-format.exe` | Rust | main:xingcheng/src/backend/rust/xc-format/Cargo.toml |
| `xc-format.exe` | Rust | rag:xingcheng/src/backend/rust/xc-format/Cargo.toml |
| `xc-format.exe` | Rust | ui:xingcheng/src/backend/rust/xc-format/Cargo.toml |
| `xc-learning.exe` | C# | devin:xingcheng/src/backend/csharp/GPTBridge.XingchengLearning/GPTBridge.XingchengLearning.csproj |
| `xc-learning.exe` | C# | git:xingcheng/src/backend/csharp/GPTBridge.XingchengLearning/GPTBridge.XingchengLearning.csproj |
| `xc-learning.exe` | C# | local-model:xingcheng/src/backend/csharp/GPTBridge.XingchengLearning/GPTBridge.XingchengLearning.csproj |
| `xc-learning.exe` | C# | main:xingcheng/src/backend/csharp/GPTBridge.XingchengLearning/GPTBridge.XingchengLearning.csproj |
| `xc-learning.exe` | C# | rag:xingcheng/src/backend/csharp/GPTBridge.XingchengLearning/GPTBridge.XingchengLearning.csproj |
| `xc-learning.exe` | C# | ui:xingcheng/src/backend/csharp/GPTBridge.XingchengLearning/GPTBridge.XingchengLearning.csproj |
| `xc-runtime-host.exe` | Rust | devin:xingcheng/src/backend/rust/xc-runtime-host/Cargo.toml |
| `xc-runtime-host.exe` | Rust | git:xingcheng/src/backend/rust/xc-runtime-host/Cargo.toml |
| `xc-runtime-host.exe` | Rust | local-model:xingcheng/src/backend/rust/xc-runtime-host/Cargo.toml |
| `xc-runtime-host.exe` | Rust | main:xingcheng/src/backend/rust/xc-runtime-host/Cargo.toml |
| `xc-runtime-host.exe` | Rust | rag:xingcheng/src/backend/rust/xc-runtime-host/Cargo.toml |
| `xc-runtime-host.exe` | Rust | ui:xingcheng/src/backend/rust/xc-runtime-host/Cargo.toml |
| `xcorpus.dll` | Rust | devin:xingcheng/src/backend/rust/xcorpus/Cargo.toml |
| `xcorpus.dll` | Rust | git:xingcheng/src/backend/rust/xcorpus/Cargo.toml |
| `xcorpus.dll` | Rust | local-model:xingcheng/src/backend/rust/xcorpus/Cargo.toml |
| `xcorpus.dll` | Rust | main:xingcheng/src/backend/rust/xcorpus/Cargo.toml |
| `xcorpus.dll` | Rust | rag:xingcheng/src/backend/rust/xcorpus/Cargo.toml |
| `xcorpus.dll` | Rust | ui:xingcheng/src/backend/rust/xcorpus/Cargo.toml |
| `xcorpus.exe` | Rust | devin:xingcheng/src/backend/rust/xcorpus/Cargo.toml |
| `xcorpus.exe` | Rust | git:xingcheng/src/backend/rust/xcorpus/Cargo.toml |
| `xcorpus.exe` | Rust | local-model:xingcheng/src/backend/rust/xcorpus/Cargo.toml |
| `xcorpus.exe` | Rust | main:xingcheng/src/backend/rust/xcorpus/Cargo.toml |
| `xcorpus.exe` | Rust | rag:xingcheng/src/backend/rust/xcorpus/Cargo.toml |
| `xcorpus.exe` | Rust | ui:xingcheng/src/backend/rust/xcorpus/Cargo.toml |
| `xct-executor.exe` | C# | devin:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/executor/XingchengTrainExecutor.csproj |
| `xct-executor.exe` | C# | git:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/executor/XingchengTrainExecutor.csproj |
| `xct-executor.exe` | C# | local-model:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/executor/XingchengTrainExecutor.csproj |
| `xct-executor.exe` | C# | main:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/executor/XingchengTrainExecutor.csproj |
| `xct-executor.exe` | C# | rag:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/executor/XingchengTrainExecutor.csproj |
| `xct-executor.exe` | C# | ui:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/executor/XingchengTrainExecutor.csproj |
| `xingcheng_trainer.exe` | C++ | devin:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/_nfguard/build_devin.bat |
| `xingcheng_trainer.exe` | C++ | devin:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/_nfguard/build_test.bat |
| `xingcheng_trainer.exe` | C++ | git:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/_nfguard/build_devin.bat |
| `xingcheng_trainer.exe` | C++ | git:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/_nfguard/build_test.bat |
| `xingcheng_trainer.exe` | C++ | local-model:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/_nfguard/build_devin.bat |
| `xingcheng_trainer.exe` | C++ | local-model:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/_nfguard/build_test.bat |
| `xingcheng_trainer.exe` | C++ | main:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/_nfguard/build_devin.bat |
| `xingcheng_trainer.exe` | C++ | main:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/_nfguard/build_test.bat |
| `xingcheng_trainer.exe` | C++ | rag:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/_nfguard/build_devin.bat |
| `xingcheng_trainer.exe` | C++ | rag:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/_nfguard/build_test.bat |
| `xingcheng_trainer.exe` | C++ | ui:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/_nfguard/build_devin.bat |
| `xingcheng_trainer.exe` | C++ | ui:xingcheng/src/backend/services/xingcheng/infrastructure/native_transformer/training/_nfguard/build_test.bat |
| `xstore.exe` | Rust | devin:xingcheng/src/backend/rust/xstore/Cargo.toml |
| `xstore.exe` | Rust | git:xingcheng/src/backend/rust/xstore/Cargo.toml |
| `xstore.exe` | Rust | local-model:xingcheng/src/backend/rust/xstore/Cargo.toml |
| `xstore.exe` | Rust | main:xingcheng/src/backend/rust/xstore/Cargo.toml |
| `xstore.exe` | Rust | rag:xingcheng/src/backend/rust/xstore/Cargo.toml |
| `xstore.exe` | Rust | ui:xingcheng/src/backend/rust/xstore/Cargo.toml |
<!-- /autogen:xingcheng-binaries -->

## 治理邊界

- 星澄 deny：`governance-rule`、`direct-database-write`、`main-program`、`other-tools`；manifest `direct_instruction: PERMISSION_DENIED`。
- 部署釘選：`xingcheng/runtime/settings/native-engine.json::checkpoint` → `serve --bundle`；未釘選 fail-closed（`XC_BUNDLE_CHECKPOINT_UNPINNED`）。
- 消費者政策：`csharp-orchestrator-client-only`；`/v1/infer` 需 session token；`request_channel` 走 ChannelHost。
- 資源邊界：`native/resource_governor` 為主系統唯一全機資源權威（B3/B16/B159/A610）；星澄僅持 `ResourceGovernorClient`（Request/Renew/Release/Report 檔案契約）＋ `XingchengLocalResourceAllocator`（grant 內部分配，非 governor）；governor 不可用 → autonomous training fail-closed。
- Native-only 閘門：`--native-only-check`（star-native-only-gate/v1 → release gate `NATIVE_ONLY_GATE`）＋ `--cuda-native-check`（star-cuda-native-check/v1：`driver_api_only`、禁止 cudart/cublas/cudnn/nvrtc 靜動態連結、in-tree PTX kernel 計數）。
- 全 lane fail-closed typed error codes（`EXECUTOR_*`、`KERNEL_POLICY_DENIED`、`MODE_NOT_REGISTERED`、`XC_*`、`RESOURCE_*`）。
