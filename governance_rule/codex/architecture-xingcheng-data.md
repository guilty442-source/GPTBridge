# 星澄／完整資料圖

> 規範性檢視檔。本檔只鏡像資料格式、權責與流向，不複製任何權威資料內容。格式 tag、schema 與欄位以權威源（xstore/CONTRACT.md、xcb.rs、xcn1.rs、Repository.cs、Policy.cs）為準。

## 資料流總覽

```mermaid
flowchart LR
    subgraph SRC["資料來源"]
        RDB["role DBs<br/>language_training_example<br/>language_preference_pair"]
        CORPUS["corpus registry + file roots"]
        TEACHER["Ollama teacher"]
    end

    subgraph PIPE["資料管線"]
        SNAP["SftDataset →<br/>star-transformer-sft/pretrain/dpo JSONL"]
        XCORP["xcorpus corpus →<br/>train/valid-ids.xcb + documents/records/cache/manifest"]
        TOK["xc_modeltool tokenize →<br/>XCB1 star-token-batch"]
    end

    subgraph PG["PostgreSQL gptbridge_xingcheng"]
        DS["transformer_training_dataset(+example)<br/>immutable snapshot"]
        JOB["transformer_training_job<br/>queued→preflight→training→validating→completed"]
        ADP["adapter_candidate/evaluation/release"]
        AUD["transformer_training_audit_event<br/>sha256 chain"]
    end

    subgraph JDIR["runtime/models/jobs/&lt;id&gt;/"]
        JJ["job.json / model-config.json"]
        XCB["train-ids.xcb / val-ids.xcb"]
        XCN["init.xcn → final.xcn<br/>star-native-ckpt/v1 (XCN10)"]
        BND["bundle/{manifest,weights.bin,tokenizer}<br/>star-native-inference-bundle"]
        RPT["report.json<br/>star-native-train-report/v1"]
    end

    subgraph STORE["xstore &lt;store&gt;/"]
        OBJ["objects/&lt;sha2&gt;/&lt;sha256&gt;.bin"]
        IDX["store-index.jsonl (prev-chain)"]
        SNPM["snapshots/&lt;sha&gt;.json"]
        AUDP["store-audit.jsonl"]
        POOL["pool-&lt;class&gt;.jsonl ×15"]
    end

    subgraph STATE["runtime/state + lifecycle"]
        LC["lifecycle.json<br/>star-model-lifecycle/v1"]
        PIN["native-engine.json 釘選"]
        ST["self-learning/maturation/maturity state"]
    end

    subgraph SERVE["推論資料物"]
        XPA["XPA1 prefill artifact"]
        XSST["XSST delta-state blob"]
        XEB["XEB1 mapped expert store"]
    end

    RDB --> SNAP --> DS --> JOB
    CORPUS --> XCORP --> XCB
    TEACHER --> RDB
    JOB --> JJ
    JOB --> XCB
    XCB --> TRAIN["xingcheng_trainer"] --> XCN
    TRAIN --> RPT
    XCN -->|export-bundle| BND
    SNAP -->|xstore put| OBJ
    XCORP -->|xstore snapshot| SNPM
    JOB --> ADP --> LC --> PIN --> SERVE
    OBJ --- IDX
    EVALF["eval/capability reports"] --> POOL
```

## 二進位容器家族

<!-- autogen:xingcheng-containers -->
*autogen-scanner/v1 · 2108 files · main+devin+git+local-model+rag+ui*
| Magic | 定義/引用來源 |
|---|---|
| `XCB1` | devin:Standalone tools/local-model/contracts/xnc/vectors/manifest.json, devin:Standalone tools/local-model/src/backend/cpp/src/xcb_batch.h, devin:Standalone tools/local-model/src/backend/rust/xcorpus/src/xcb.rs, Standalone tools/local-model/contracts/xnc/vectors/manifest.json |
| `XCN1` | devin:Standalone tools/local-model/contracts/xnc/vectors/manifest.json, devin:Standalone tools/local-model/src/backend/rust/xc-format/src/lib.rs, devin:Standalone tools/local-model/src/backend/rust/xstore/src/xcn1.rs, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+13) |
| `XEB1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_scale.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_scale.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_scale.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_scale.h (+2) |
| `XPA1` | devin:Standalone tools/local-model/src/backend/cpp/src/engine_generate.h, git:Standalone tools/local-model/src/backend/cpp/src/engine_generate.h, local-model:Standalone tools/local-model/src/backend/cpp/src/engine_generate.h, Standalone tools/local-model/src/backend/cpp/src/engine_generate.h (+2) |
| `XSST` | devin:Standalone tools/local-model/src/backend/cpp/src/engine_generate.h, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_scale.h, git:Standalone tools/local-model/src/backend/cpp/src/engine_generate.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_scale.h (+8) |
<!-- /autogen:xingcheng-containers -->

| Magic | format tag | 生產者 | 內容 |
|---|---|---|---|
| `XCN1` | `star-native-ckpt/v1` | trainer `ckpt_save` | versioned config + tensor table (name/dims/f32)；v10 加 mtp 欄位 + gemma4 marker |
| `XCB1` | `star-token-batch/v1` | xcorpus / modeltool tokenize | meta JSON + records{kind 1-4, ids, labels, rej, vision} |
| `XPA1` | `star-prefill-artifact/v1` | engine `prefill_artifact` | token_ids + KV + XSST + logits + sha256 tail |
| `XSST` | （內嵌子 blob） | engine | delta-state |
| `XEB1` | `star-mapped-expert-store/v1` | expert-store-build | 128B index entries + INT8 expert payloads + sha |

不存在（勿當既有 contract）：`XDS`/`XCR`/`XST`/`XEV`；`.pt` 退役（fail-closed `EXECUTOR_INIT_CHECKPOINT_RETIRED`）。

## xstore 儲存佈局

```
<store>/
├── objects/<sha2>/<sha256>.bin    immutable content-addressed bodies
├── tmp/*.tmp                      fsync+rename spill
├── store-index.jsonl              star-store-index/v1, prev-chained receipts
├── snapshots/<manifest-sha>.json  star-dataset-snapshot/v1
└── store-audit.jsonl              star-audit-log/v1 (prev=sha256 of prev raw line)
```

## corpus 輸出佈局

```
<out>/
├── train-ids.xcb / valid-ids.xcb   XCB1 kind=1, meta{producer,packing_max_len,tokenizer_sha256}
├── documents.jsonl                 fixed field order; dataset_version=sha256(本檔)
├── train-records.jsonl / valid-records.jsonl   {path, sha256=overlap_sha}
├── corpus-cache.jsonl              star-corpus-file-cache/v1 (ids_b64; legacy ids readable)
└── manifest.json                   star-pretrain-corpus/v1 (counts/languages/integrity)
```

## job 目錄佈局

```
jobs/<id>/
├── train-src.jsonl / val-src.jsonl   來源行（{prompt,completion}|{text}|{prompt,chosen,rejected}）
├── train-ids.xcb / val-ids.xcb       tokenize 輸出（trainer 只用 train）
├── init.xcn                          bundle → XCN（init 為 bundle 時）
├── job.json / model-config.json      trainer 規格 + scratch manifest
├── report.json                       star-native-train-report/v1
├── train-stderr.log
├── final.xcn                         XCN10 checkpoint
└── bundle/                           manifest.json + weights.bin + tokenizer.json
                                     (star-native-inference-bundle/v1 + provenance block)
```

## JSONL/JSON artifact 索引（producer → consumer）

<!-- autogen:xingcheng-formats -->
*autogen-scanner/v1 · 2108 files · main+devin+git+local-model+rag+ui*
| format tag | 來源檔 |
|---|---|
| `coding-agent-trajectory/v1` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs (+2) |
| `grpo` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_job.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_job.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_job.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_job.h (+2) |
| `parity/v1` | devin:Standalone tools/local-model/src/backend/rust/xcorpus/src/xcb.rs |
| `sft` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_job.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_job.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_job.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_job.h (+2) |
| `star-agent-trajectory/v1` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs (+2) |
| `star-architecture-justification/v1` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/ArchitectureGate.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/ArchitectureGate.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/ArchitectureGate.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/ArchitectureGate.cs (+2) |
| `star-artifact-registry/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h (+2) |
| `star-audit-log/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/audit.rs |
| `star-bf16-certification/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_bf16cert.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_bf16cert.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_bf16cert.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_bf16cert.h (+2) |
| `star-bundle-provenance/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_integration.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_integration.h (+8) |
| `star-canonical-effective/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_canon.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_canon.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_canon.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_canon.h (+2) |
| `star-capacity-metrics/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h (+2) |
| `star-ckpt-converge/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-compute-plane/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_computeplane.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_computeplane.h |
| `star-corpus-file-cache/v1` | devin:Standalone tools/local-model/src/backend/rust/xcorpus/src/corpus.rs, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_corpus.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_corpus.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_corpus.h (+3) |
| `star-cpu-gemm-benchmark/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_tpu.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_tpu.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_tpu.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_tpu.h (+2) |
| `star-cuda-memory-telemetry/v1` | devin:Standalone tools/local-model/src/backend/cpp/src/cuda_memplane.h, git:Standalone tools/local-model/src/backend/cpp/src/cuda_memplane.h, local-model:Standalone tools/local-model/src/backend/cpp/src/cuda_memplane.h, Standalone tools/local-model/src/backend/cpp/src/cuda_memplane.h (+2) |
| `star-cuda-parity/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-dataset-snapshot/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/snapshot.rs |
| `star-delta-state/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp (+2) |
| `star-depth-telemetry/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_integration.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_integration.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_integration.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_integration.h (+2) |
| `star-duplicate-weight-cost/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h (+2) |
| `star-expert-granularity/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h (+2) |
| `star-expert-offload-bench/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h (+2) |
| `star-expert-residency/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h (+2) |
| `star-fim/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp (+2) |
| `star-grounded-result/v2` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/CommunityChecks.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/CommunityChecks.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/CommunityChecks.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/CommunityChecks.cs (+2) |
| `star-hardware-baseline-300m/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-hw-capability/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-kernel-policy` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_kernels.h, Standalone tools/local-model/runtime/settings/kernel-policy.json |
| `star-kernel-registry` | devin:Standalone tools/local-model/src/backend/rust/xcorpus/src/kernels.rs, devin:Standalone tools/local-model/src/backend/rust/xstore/src/kernels.rs, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_kernels.h |
| `star-kv-outer-gather-probe/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-memory-report/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-mode-registry/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp (+2) |
| `star-model-feature-catalog/v1` | devin:Standalone tools/local-model/runtime/settings/feature-catalog.json |
| `star-moe-routing-analysis/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_integration.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_integration.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_integration.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_integration.h (+2) |
| `star-moe-trace/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-mtp-bench/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_system1.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_system1.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_system1.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_system1.h (+2) |
| `star-mtp-draft-probe/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp (+2) |
| `star-native-state/v2` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-native-thinking-eval/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-native-thinking/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-parameter-efficiency/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h (+2) |
| `star-parameter-freeze-map/v1` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/SiliconChecks.cs, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/SiliconChecks.cs, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h (+8) |
| `star-parameter-reuse-probe/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-pd-bench/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h (+2) |
| `star-precision-parity/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-prefill-artifact/v1` | devin:Standalone tools/local-model/src/backend/cpp/src/engine_generate.h, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, git:Standalone tools/local-model/src/backend/cpp/src/engine_generate.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp (+8) |
| `star-prefix-smoke/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h (+2) |
| `star-quant-cert/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_quantcert.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_quantcert.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_quantcert.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_quantcert.h (+2) |
| `star-rag-prefix-bench/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_efficiency.h (+2) |
| `star-recurrent-drift/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-retention-policy/v1` | Standalone tools/local-model/runtime/settings/retention.json |
| `star-self-correction/v1` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/ConvergenceChecks.cs, devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/ConvergenceChecks.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs (+8) |
| `star-self-learning-policy/v1` | devin:Standalone tools/local-model/runtime/settings/self-learning.json, Standalone tools/local-model/runtime/settings/self-learning.json |
| `star-sequence-scheduler/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-sequence-state-bench/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-silicon-profile/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h (+2) |
| `star-silicon-routing/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h (+2) |
| `star-sparse-attention-probe/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-speculative-decoder/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_rtgates.h (+2) |
| `star-store-index/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/store.rs |
| `star-system-reuse/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_silicon.h (+2) |
| `star-system1-benchmark/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_s1bench.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_s1bench.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_s1bench.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_s1bench.h (+2) |
| `star-system1-head/v1` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_s1bench.h, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_system1.h, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs (+14) |
| `star-system1-resource/v1` | devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_system1.h, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_system1.h, local-model:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_system1.h, Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_system1.h (+2) |
| `star-teacher-distillation-policy/v1` | Standalone tools/local-model/runtime/settings/teacher-distillation.json |
| `star-token-batch/v1` | devin:Standalone tools/local-model/src/backend/rust/xcorpus/src/corpus.rs, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xc_modeltool.cpp, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_corpus.h |
| `star-trainer-probe-report/v1` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/ConvergenceChecks.cs, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_canon.h, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/ConvergenceChecks.cs, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/training/xct_canon.h (+8) |
| `star-transformer-sft/v1` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/ConvergenceGate.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/ConvergenceGate.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/ConvergenceGate.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/ConvergenceGate.cs (+2) |
| `star-typed-decision/v1` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs, devin:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_system1.h, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/LayaMiMoChecks.cs, git:Standalone tools/local-model/src/backend/services/xingcheng/infrastructure/native_transformer/tools/xcm_system1.h (+8) |
| `star-xcn-header/v1` | devin:Standalone tools/local-model/src/backend/rust/xc-format/src/main.rs, Standalone tools/local-model/src/backend/rust/xc-format/src/main.rs |
| `xnc-vectors/v1` | devin:Standalone tools/local-model/contracts/xnc/vectors/manifest.json, Standalone tools/local-model/contracts/xnc/vectors/manifest.json |
| `xstore-audit-append/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/audit.rs |
| `xstore-audit-verify/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/audit.rs |
| `xstore-ckpt-diff/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/diff.rs |
| `xstore-ckpt-info/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/main.rs |
| `xstore-ckpt-verify/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/main.rs |
| `xstore-fail-list/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/failpool.rs |
| `xstore-fail-mark/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/failpool.rs |
| `xstore-get/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/main.rs |
| `xstore-hash/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/main.rs |
| `xstore-put/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/main.rs |
| `xstore-snapshot-verify/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/snapshot.rs |
| `xstore-snapshot/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/snapshot.rs |
| `xstore-verify-store/v1` | devin:Standalone tools/local-model/src/backend/rust/xstore/src/store.rs |
| `{HOST_FORMAT}` | devin:Standalone tools/local-model/src/backend/rust/xc-runtime-host/src/main.rs, Standalone tools/local-model/src/backend/rust/xc-runtime-host/src/main.rs |
<!-- /autogen:xingcheng-formats -->

| 檔案 | format | 生產者 → 消費者 |
|---|---|---|
| snapshot JSONL | `star-transformer-sft/pretrain/dpo` | SftDataset → trainer data.path |
| documents/records/cache | `star-corpus-file-cache/v1` 等 | xcorpus → capability overlap gate |
| corpus manifest | `star-pretrain-corpus/v1` | xcorpus → C#/評估 |
| store-index/audit/snapshot | `star-store-index`/`star-audit-log`/`star-dataset-snapshot` | xstore 自鏈 |
| report.json | `star-native-train-report/v1` | trainer → JobExecutor（+resource enrichment） |
| eval/capability report | `star-native-eval-suite`/`star-capability-eval` | modeltool → adapter_evaluation + failure pool |
| lifecycle.json | `star-model-lifecycle/v1` | Lifecycle.cs ↔ 全治理 lane |
| model-service.json | `star-model-service-descriptor/v1` | ToolHost → consumers |
| self-learning.json | `star-self-learning-state/v1` | SelfLearning 自讀寫 |
| model-maturation-300m.json | `star-300m-maturation-state/v1` | Maturation300M |
| runtime-host owner.lock / artifact-registry.json | `star-runtime-host`/`star-artifact-registry` | SiliconRuntime |
| pool-*.jsonl | `star-capability-failure-pool/v1` | C# FailurePool ⇄ Rust xstore（互通 canonical JSON） |
| retention/self-learning-*.json(l) | 報告/收據 | logs/ 稽核 |

## PostgreSQL 表（`gptbridge_xingcheng`）

<!-- autogen:xingcheng-pgtables -->
*autogen-scanner/v1 · 2108 files · main+devin+git+local-model+rag+ui*
| 表 | 定義/引用來源 |
|---|---|
| `transformer_adapter_candidate` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+2) |
| `transformer_adapter_evaluation` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Evaluation.cs, devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Evaluation.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+8) |
| `transformer_adapter_registry` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+2) |
| `transformer_adapter_release` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+2) |
| `transformer_audit_immutable` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+2) |
| `transformer_dataset_snapshot_immutable` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+2) |
| `transformer_runtime_model_state` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+2) |
| `transformer_schema_metadata` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, devin:main-system/config/sql-schema-contract.json, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:main-system/config/sql-schema-contract.json (+8) |
| `transformer_training_audit_event` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+2) |
| `transformer_training_dataset` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+2) |
| `transformer_training_dataset_example` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+2) |
| `transformer_training_dataset_snapshot_key` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+2) |
| `transformer_training_job` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/GenerationMigration.cs, devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/GenerationMigration.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+8) |
| `transformer_training_repository` | devin:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, git:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, local-model:Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs, Standalone tools/local-model/src/backend/csharp/GPTBridge.XingchengLearning/Repository.cs (+2) |
<!-- /autogen:xingcheng-pgtables -->

| 表 | 關鍵約束 |
|---|---|
| `transformer_schema_metadata` | schema_version key |
| `transformer_training_dataset` | UNIQUE(content_sha256,snapshot_sha256)；UPDATE/DELETE trigger 禁改（snapshot immutable） |
| `transformer_training_dataset_example` | PK(dataset_id,ordinal)；UNIQUE(dataset_id,content_sha256) |
| `transformer_training_job` | status 狀態機 queued→preflight→training→validating→completed/failed/cancelled |
| `transformer_adapter_candidate` | artifact_sha256 UNIQUE；partial UNIQUE on status=active |
| `transformer_adapter_evaluation` | UNIQUE(adapter_id,suite_sha256) |
| `transformer_adapter_release` | action∈stage/activate/rollback/retire |
| `transformer_runtime_model_state` | singleton；automatic_weight_replacement 必為 0 |
| `transformer_training_audit_event` | sha256 鏈（prev=前筆 canonical hash）；UPDATE+DELETE trigger immutable |

role DBs：`gptbridge_xingcheng_{main,investment,mathematical,coding}`（唯讀收集）。
identity：`xingcheng_identity`（RLS，`gptbridge_xingcheng_internal` 唯一內部 role）。

## 資料邊界

- 不受信任輸入（checkpoint、manifest、語料、大 binary）只經 Rust lane 剖析。
- 稽核鏈三處並存：PG `audit_event` 鏈、xstore `store-audit.jsonl`、治理 JSONL；尾端截斷需外部錨定。
- 版本政策：新 contract 不釘版本（`star-kernel-*`）；既有 `/vN` 為註冊名稱本體。
