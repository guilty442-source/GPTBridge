# C++ W1 Q×K SIMD + scores 緩衝（P3d）

> 對應 `native/core/runtime_core` 為基礎，`Standalone tools/local-model/src/backend/cpp/src/engine.cpp:1260` 的 `k_t` 轉置 + `matmul` + `softmax` 為熱點

## 現況

- `engine.cpp:1260-1280`：每 head 重新分配 `k_t`（`head_dim * total_len`）、`scores`（`seq * total_len`）、`k_all`/`v_all` 等，`matmul` 經 C ABI `gptbridge_native_transformer_matmul`
- 未做：Q×K 以 C++ SIMD（AVX2/AVX-512）直接計算、scores 預配置重用、FlashAttention 分塊

## 計畫（W1）

1. **scores 緩衝預配置**：`NativeInferenceEngine` 內置 `scores_pool`（`GPTBRIDGE_NATIVE_POOL_MAX_BLOCKS` 64，`mem_pool` per `memory.h:202`），`forward_hidden` 進頭部前 `pool_acquire`，結束 `pool_release`
2. **Q×K SIMD**：C++ 層以 `__m256d` / `__m512d` 實作 `q_head * k_t` 的 blocked GEMM（`k` 維分塊 64），與 `native/core/vector.c` 的 `matmul` 對照，logits 差 ≤1e-3
3. **驗收**：`native/test_suites` 等價門檻 + `prefill` 延遲對比（無 AVX 機器自動回退純量）

## 依賴

- `native/core/memory.h` 池化 + `native/include/gptbridge_native.h` C ABI
- 本重構不改變 Python 路徑（shadow），先建基準再優化（法典 A583）

## 下一步

- 以 `engine.cpp` 的 `matmul` 路徑為基線，量測 `prefill 512` 延遲與配置次數，再做 W1 分塊

## 實作結果（2026-09-22，證據 `c-w1-qk-simd-evidence-20260922.json`）

- 已落地：每頭一次解析 K/V 來源指標陣列（取代逐元素 `kv_slot` 查表）、移除 `k_all`/`v_all`/`k_t`/`head_out` 每頭暫存、`scores` 提升為每層單一重用緩衝、Q·K 改 streaming `dot_f64`、P·V 改 `axpy_f64` 直接累加進 `attn_flat`、AVX2 `__m256d` 核心＋`cpuid`/`xgetbv` 執行期偵測＋純量回退（masked 位置完全跳過不計算）
- 與原計畫差異：scores 用每層重用 `std::vector` 而非 `mem_pool`（語義相同、零 C ABI 改動）；blocked GEMM 改 streaming dot＋axpy（head_dim=64 時記憶體配置收益更大且數值更貼近原實作）
- 等價：top-32 logits 順序全同、最大差 1.5e-14（門檻 1e-3）、gen4 token 全同；KV cache 5/5、C↔C++ 一致性 4/4 PASS
- 延遲（共載主機、min-of-runs）：prefill256 3609→2718 ms（−24.7%）、prefill512 6234→5750 ms（−7.7%）
- 未做（後續）：FlashAttention 分塊、AVX-512 `__m512d`、跨層 scores pool 上限管控
