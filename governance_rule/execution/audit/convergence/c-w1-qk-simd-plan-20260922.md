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
