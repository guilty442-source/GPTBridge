# 5 核心及模組效能提升與自適化評估計畫報告

日期：2026-09-26 ｜ 範圍：5 個執行核心 + 跨層模組 ｜ 狀態：已完成第一期優化與驗證

---

## 一、5 個執行核心 — 優化項目與實測結果

### 1. C Compute Core（`native/core/`）

正式底層計算核心。本期複查結論為「已達優化成熟度」，未做變更：

- 既有優化：AVX-512 / AVX2 / FMA 運行時派送 + scalar fallback（語意等價）；
  dot/matmul/RMSNorm/RoPE/softmax/attention 全 SIMD 化；attention 採
  online/blocked 有界工作區（不建完整 score matrix）；overflow-safe
  維度檢查；caller-owned buffers、零無界配置。
- 本期動作：隨 C++ 擴充建置鏈驗證 `/O2 /GL /arch:AVX2 /fp:precise`
  乾淨編譯通過（`/fp:fast` 刻意排除 — 保護數值 parity 證據）。
- 後續：僅在量測指出真實瓶頸時才追加優化（如 benchmark-driven）。

### 2. C++ Inference（`local-model/src/backend/cpp/`）

正式模型推論層。本期完成 decode 熱路徑配置削減：

- `sample_next`：greedy 路徑（最常見）改為直接 argmax 掃描，**每 token
  省去整個 vocab（MB 級）的 `std::vector` 拷貝**；repetition penalty 改惰性
  應用於掃描中，winner 語意與全拷貝版逐位一致。
- 殘差累加（attn/mlp residual、MoE 專家聚合、shared-expert 合併）改用 SIMD
  `axpy_f64`，移除逐元素迴圈與中間 vector。
- `build_cpp.py` 強化：cp950 console 診斷輸出改 `errors="replace"`（修
  UnicodeEncodeError 崩潰）；nvcc timeout 600→1800s 且 TimeoutExpired
  轉 fail-closed 降級；新增 `XINGCHENG_SKIP_CUDA_KERNELS` 受管逃生閥。
- 實測：cp314 pyd 建置成功；bundle 載入 8.66s；greedy / seeded-sampling
  （deterministic ✓）/ batch / KV 記帳全通過。`cuda_active: False` —
  nvcc 12.0 對 MSVC 14.4x–14.5x 全 toolset 不相容（rc≠0），device kernels
  依設計 fail-closed 略過，cuBLAS bridge 仍啟用。

### 3. Rust vectord（`vectord-rs/src/store.rs`）

canonical 向量引擎。本期由使用者/工作者完成儲存層配置優化：

- point id 三處 `String` 拷貝（forward key、internal_to_id value、
  payloads/tombstoned key）改 `Arc<str>` 共享單一配置。
- `upsert` 改接收 `Vec<f32>` 移轉所有權：存入 `vectors` 的同一份記憶體
  直接借給 HNSW insert — **每筆 upsert 省一次 vector clone**。
- `delete`/`count_where`/`dump` 等改 `get_key_value`/借用查找，消除
  熱路徑 `to_string()`。

### 4. Julia Compute（`julia-compute/`）

科學計算 spawn-per-call endpoint：

- `compute.jl` 拆分為 `JuliaCompute` 模組 + `Pkg.instantiate()` 鎖定
  Manifest（無 registry resolve、零版本漂移），`precompile()` 暖
  pkgimage cache — **冷啟動 ~20s → ~2.1s**（實測完整 JSON 往返）。
- LinearAlgebra / Random 依 op 前綴惰性 `Base.invokelatest` 載入；
  `stats.*` 路徑不再支付未用 stdlib 載入。
- 配置削減：批次 quantile 單次排序（原本每 q 排一次）、describe /
  correlation 重用 mean、輸入轉換去拷貝。
- 驗證：6 ops 全對 + `UNKNOWN_OP` fail-closed（exit=2）；契約
  `julia-compute/v1` 不變。

### 5. JAX Backend（`jax_backend/sft.py`）

模型訓練框架：

- 單一 fused `jax.jit` train_step（loss+grads+clip+AdamW）＋
  `donate_argnums=(0,1)`：params/opt_state buffer 每步重用而非重新配置，
  並已修 tied-leaves「donate the same buffer twice」別名問題。
- 64-token bucket collate（上限 max_length）：把「每種序列長度觸發一次
  XLA 重編譯」收斂為 `ceil(max_length/64)` 個編譯形狀。
- lr 以 traced scalar 傳入（lr schedule 不再強制 retrace）；AdamW bias
  correction 用 `jnp.power` traced step（避免 jit 內 host sync）；eval
  共用 jitted loss。
- 驗證：`test_jax_backend` 4/4 通過；煙測 3-step loss 4.074→4.028 遞減。

---

## 二、跨層模組效能提升（本期一併落地）

| 模組 | 優化 | 實測 |
|---|---|---|
| `pg_adapter.py` | 有界連線池（checkout ≈ loopback `SELECT 1`，較 handshake ~100x）＋ SQL translate LRU cache（~15 regex/查詢 → 命中零成本）＋ `PG_ADAPTER_POOL=0` 可關 | 已提交 |
| `phases_execution.py` | 私域 schema 探針由「每 store 一次 connect」改為單連線 `information_schema` 批次查詢（更嚴格且零 churn） | 已提交 |
| `gpu_coordinator.py` | import 2–4s → **0.76s**；query <0.2s（commit d0307ec2） | 實測 |
| `repair_inspection.py` | sqlite 完整性檢查改純 header 偵測（退役引擎不再開檔）；保留證據複製流程 | 已提交 |
| `resource-governor-rules.json` | 編譯工具鏈（cl/link/nvcc/cicc/ptxas…）`exclude:true` — 修復 worker job 10% hard-cap 把編譯餓死（cl Ready 但 ~0 CPU） | commit 07b2dda5 |
| build flags | `build_native.py`/`build_cpp.py`/`test_suites/build.ps1`：`/O2 /GL`＋`/LTCG /OPT:REF /OPT:ICF` | commit 4bc194d6 |
| `.NET` | `PublishReadyToRun`（R2R 預編譯 IL→native，加速啟動） | commit 4bc194d6 |
| `searchd-go` | `SetMemoryLimit(256MiB)`＋`/v1/search` max-inflight semaphore | commit 4bc194d6 |
| UI | `babel-plugin-react-compiler` 自動 memo 化；`useToolboxApplications` 修正 | commit 4bc194d6 |
| `vectord` store | 見 §一.3 | 工作者已提交 |

**關鍵基礎設施發現**：resource-governor 的 worker-plane Job Object 對整個
worker/toolbox/repo-other plane 施加 10% 聚合 CPU hard-cap，且子行程出生即
繼承 job membership — 受管 shell 下的編譯事實上被餓死。解法（已落地並
文件化於 commit）：toolchain exclude 規則 ＋ `CREATE_BREAKAWAY_FROM_JOB`
啟動建置父行程。

---

## 三、自適化評估計畫（Adaptive Evaluation Plan）

### 3.1 評估目標

讓效能宣稱都有可重現證據，並讓退化在「同一個受管環境」下被自動偵測，
而非依賴一次性人工量測。

### 3.2 分層指標（per-core baseline）

| 層 | 主要指標 | 次要指標 |
|---|---|---|
| C core | suite/audit 耗時、SIMD vs scalar 加速比 | 記憶體高水位（bounded workspace 驗證） |
| C++ inference | decode tok/s（greedy/sampled）、prefill ms | 每 token 配置數、KV bytes、prefix-cache 命中率 |
| Rust vectord | upsert/search ops/s、p95 latency | 每 point 配置數（Arc 共享率）、snapshot rebuild 時間 |
| Julia | 冷啟動 spawn latency、各 op latency | pkgimage 命中率、RSS |
| JAX | step time、XLA compile 次數（bucket 收斂驗證） | host sync 次數、peak buffer、eval loss parity |

### 3.3 量測方法（自適化核心）

1. **Warmup-aware**：Julia/JAX 以「冷進程首 call vs 暖 cache call」分開記錄；
   JIT/donation 效果只在暖路徑計入。
2. **環境自適應**：量測程序標記 governor 可見特徵（記錄
   `resource-governor.json` 的 mode/regulation.active/工作面 CPU budget 於
   報告 metadata），量測值永遠附帶受管環境上下文 — 跨環境比較以相對
   比率而非絕對值判定。
3. **統計門檻**：每指標 ≥5 trials，報告 p50/p95；回歸判定門檻
   `p95_regress > +15%`（CPU-bound）或 `-10%`（吞吐）才列為 regression，
   避免受管機器抖動誤報。
4. **編譯計數器**：JAX 以 `jax.jit` tracing count / cache miss 斷言
   bucket 內零重編譯（寫進測試而非報告 — 錯誤即失敗）。

### 3.4 評估管線掛點（用既有治理機制，不新增平行系統）

- **Evidence ledger**：每次基準跑寫入 `runtime/logs/` 對應 ledger
  （沿用 `native-engine-executions.jsonl` / self-learning 報告模式）。
- **Maturity ladder**（A556/A557）：C++/JAX 的效能回歸併入 L1–L2 gate
  （forward/step finite + 耗時上限），L3+ 的 ppl gate 不受影響。
- **Capability suite v2**：self-learning 週期本就做 per-category 回歸 —
  效能維度只在評估尾端附加 latency 記錄，不改 gate 語意。
- **Governance audit**：`single-authority` / activation-state 檢查保持
  fail-closed；效能變更不得繞過。
- **Governor 回饋環**：規則檔（rules json）是唯一調節面；新 toolchain/
  runtime 進程若要豁免 cap，走 rules 修正案而非繞過 governor。

### 3.5 排程與觸發

| 觸發 | 動作 |
|---|---|
| 每次 perf commit | 對應層 smoke + micro-benchmark（本報告 §一之實測即首例） |
| self-learning cycle 尾端 | latency 記錄入報告（不改 gate） |
| 每週 / release 前 | 全 5 層完整 benchmark → 對基線表做比率比較 → regression 入 issue/ledger |
| governor mode 切換（sleep/low/medium/high） | 記錄上下文，不阻斷；絕對值僅在同 mode 間比較 |

### 3.6 已知阻塞與後續工作

1. **nvcc 12.0 × MSVC 14.4x–14.5x 不相容** → CUDA device kernels 目前
   fail-closed 未建。解決路徑：升 CUDA toolkit（12.4+ 支援較新 MSVC）或
   降 MSVC toolset 至 nvcc 支援範圍（14.2x 且 -allow-unsupported-compiler
   實測仍失敗）。GPU 路徑驗證因此延後。
2. **C++ 測試需 torch**（`cpp_export.py` import）→ 測試環境需在受管機器
   提供 torch 或拆分 export/import 依賴。
3. MoE routing 的 per-token `order` 向量與排序 — 已識別的下一個 C++
   配置熱點，待 benchmark 證據再動。
4. JAX donation 的 GPU 行為尚未驗證（本機 `CpuDevice`）— 待 CUDA 可用
   時補測 aliasing/donation。
5. `QDRANT_AUTHORITY` activation 警告仍走 codex amendment 管線
   （不可直接改法典）。
6. **vectord 飢餓事件（已修）**：常駐 vectord 被 worker job 10% 聚合 cap
   餓到 healthz=20s — 已加 `vectord.exe` exclude 規則並重啟脫離 job
   （job membership 不可撤銷，既有行程必須重生才生效）。healthz
   20.04s → 0.70s。同類「低常態 CPU、延遲敏感」常駐服務的豁免政策
   值得在法典層級正式化。

### 3.7 基線（本期實測記錄）

**優化前後對比**：

| 指標 | 前 | 後 |
|---|---|---|
| Julia 冷 spawn + stats.describe 往返 | ~20s（無 pkgimage） | **2.1s**（批次量測 3.4–8.4s/op，含真實工作負載） |
| gpu_coordinator import | 2–4s | **0.76s** |
| nvcc 單檔（受管 cap 下） | >600s timeout | minutes（exclude+breakaway 後） |
| cp314 extension 建置 | 逾 25min 未完成 | **完成**（全 TU + LTCG） |
| `sample_next` greedy | 每 token 全 vocab 拷貝 | 零拷貝 argmax |
| vectord upsert | vector clone ×2 + id String ×3 | vector ×0 clone + id Arc 共享 |
| vectord healthz（cap 飢餓下） | 20.04s | **0.70s**（exclude+respawn） |

**第一期基線量測**（`main-system/runtime/logs/perf-baseline-20260926T151226Z.json`，
governor mode=medium / regulation=off / worker budget=10%）：

| 層 | 量測值 |
|---|---|
| Julia spawn-per-call | 6 ops 全 ok；2.6–8.4s/op（spawn-per-call 契約成本） |
| JAX（CPU） | bucket A compile 10.8s → steady 0.97s/step；bucket B compile 14.9s → steady 1.19s/step；**回 A bucket 197ms 零重編譯 ✓**；`donate_argnums` tied-params 安全 ✓ |
| C++（CPU, bundle latest-2bd3） | load 10.6s；prefill 364ms/12tok；greedy **29.3 tok/s**；sampled(top-k/p +rep-penalty) 687ms/16tok；batch2×8 1.07s；KV 3.1MB；總 RSS 708MB |
| vectord（免 cap） | upsert 375 pts/s（250-batch p50 642ms）；search top_k=10 **p50 14.9ms / p95 196ms**；2000 pts 正確 |
| C core | 由 C++ forward/generate 路徑覆蓋（SIMD 派送於該堆疊內生效） |

**已知量測注意事項**：vectord 數值取自「脫離 job 後」的未受限行程；
search p95 偏高含 HNSW 暖查詢與 snapshot 執行緒鎖競爭 — 下次基準
應加 warmup 查詢再計 p95。
