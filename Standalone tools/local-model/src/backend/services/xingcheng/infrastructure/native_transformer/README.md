# 星澄原生模型 (XingCheng Native Model)

「星澄」本地原生 AI 模型架構 — 高效能、可擴充、可逐層下沉最佳化的
PyTorch Transformer 原生實作。

## 架構總覽

```
星澄原生模型
                      │
          ┌───────────┴───────────┐
          │                       │
      Training                 Inference
          │                       │
       Python                 Python API
          │                       │
       PyTorch              Native Dispatch
          │                       │
       GPU                    Python / C++
          │                       │
     Model Weights           Generated Tokens
```

訓練以 Python + PyTorch 為主線直通 GPU；推論以 Python API 為入口，
由 Native Dispatch 在 Python ／ C++ 雙路徑間調度產出 Token。

## 正式主線技術棧

```
星澄
  → Python
  → PyTorch (nn.Module / Tensor / Autograd)
  → Transformer (Embedding / Attention / MLP / RMSNorm / Residual / LM Head / Sampling)
  → Tensor Operations (GEMM / Softmax / Reduction / Activation / Gather / Scatter ...)
  → Computation Graph + Autograd (Backward Graph / Gradient / Optimizer)
  → ATen / Dispatcher / Torch C++ Backend
  → 高效能數學與 Kernel (BLAS / oneDNN / cuBLASLt / cuDNN / FlashAttention)
  → Triton / Gluon / CUDA (自研 GPU Kernel)
  → PTX / SASS (極端底層最佳化)
  → CPU / NVIDIA GPU (Apple MPS 為未來支線)
```

## 設計原則

1. **優先使用成熟高效函式庫，不重複造輪子。**
2. **訓練以 Python + PyTorch 為主線**：模型設計、Computation Graph / Autograd、Optimizer
   與訓練流程一律留在 PyTorch 高階層，不下沉到 C++。
3. **推論允許 Python／C++ 雙路徑**：Python 路徑（PyTorch / Triton / 內部程式碼）為預設與
   參考實作；C++ 路徑（`native/` 標準樹 pybind11 延伸 / Gluon / CUDA C++）為高吞吐、
   低延遲的部署選項。兩條路徑共用同一組權重、tokenizer 與語意契約，C++ 路徑僅在推論生效。
4. **只有實際效能瓶頸時才逐層下沉**（僅限推論路徑）。
5. Python 負責模型設計與高階控制；PyTorch 負責 Tensor / Autograd / 執行框架；
   ATen / C++ 負責底層 Tensor 與 Runtime；cuBLASLt / cuDNN / FlashAttention
   負責成熟高效數學運算；Triton / Gluon 負責星澄自研 GPU Kernel；
   CUDA / PTX 負責極低階 NVIDIA GPU 最佳化。

## 第一版範圍（V1 Scope）

模型本身以 **Python + PyTorch 全棧**建立，不自行開發 C++ Tensor Library：

```
星澄原生模型
    │
    ├─ Python
    │   ├─ Tokenizer 接入
    │   ├─ Transformer 架構
    │   ├─ Attention
    │   ├─ Feed Forward
    │   ├─ Training Loop
    │   ├─ Optimizer
    │   ├─ Checkpoint
    │   └─ Inference API
    │
    └─ PyTorch
        ├─ Autograd
        ├─ ATen / C++ Backend
        └─ CUDA / GPU Acceleration
```

PyTorch 已提供 ATen / C++ Backend 與 CUDA / GPU 加速，第一版只撰寫 Python 模型、
訓練流程與 Inference API，直接使用其底層高效能運算；第一版的驗收目標是
**能訓練、能生成文字、能儲存權重並重新載入**。

| V1 項目 | 對應實作 |
|---------|----------|
| Tokenizer 接入 | `tokenizer.py`, `bpe.py` |
| Transformer 架構 | `modules/model.py`, `config.py` |
| Attention | `modules/attention.py`（MHA / GQA + RoPE + SDPA） |
| Feed Forward | `modules/mlp.py`（SwiGLU） |
| Training Loop | `training/trainer.py`, `training/pretrain.py`, `training/sft.py`, `training/dpo.py` |
| Optimizer | `training/optimizer.py`（AdamW / SGD，PyTorch 內建） |
| Checkpoint | `checkpoint.py`（存讀、原子寫入、雜湊驗證、續訓） |
| Inference API | `inference/*.py`（KV cache / sampler / generate） |
| Autograd / ATen / CUDA | 由 PyTorch 提供 |

**不在 V1 範圍**：自研 C++ Tensor Library、自寫 GEMM / CUDA Kernel（Triton / Gluon /
CUDA C++ 為後續效能下沉階段）；推論的 C++ 路徑（`native/` 標準樹）屬於後續階段，不在此列。

## 套件結構

```
native_transformer/
├── __init__.py              # 公開 API：XingChengConfig / XingChengForCausalLM / XingChengTokenizer
├── config.py                # XingChengConfig（small / base / large 預設）
├── tokenizer.py             # 位元組級 tokenizer（無外部依賴）
├── execution/               # 執行層：裝置 / 後端 / 記憶體
│   ├── backend.py           #   CUDA / MPS / Triton / FlashAttention 能力偵測 + GEMM 後端選址
│   └── memory.py            #   記憶體管理 + context window 逐級降級
├── kernels/                 # 自研 GPU Kernel 層（Triton → PyTorch fallback）
│   ├── rmsnorm.py           #   RMSNorm（Triton kernel + PyTorch 參考）
│   ├── rope.py              #   RoPE（Triton kernel + PyTorch 參考）
│   ├── swiglu.py            #   SwiGLU gate（Triton kernel + PyTorch 參考）
│   ├── quant.py             #   Quantization / Dequantization + INT4 packing（Triton + PyTorch 參考）
│   └── tensor_ops.py        #   Tensor Ops：GEMM / Softmax / Activation / Gather / Scatter / Reduction（Triton + PyTorch 參考）
├── modules/                 # Transformer 核心模組（nn.Module）
│   ├── embedding.py         #   Token + Position Embedding
│   ├── norm.py              #   RMSNorm / LayerNorm 統一介面
│   ├── attention.py         #   MHA / GQA + RoPE + FlashAttention (SDPA)
│   ├── mlp.py               #   SwiGLU / 標準 FFN
│   ├── transformer_block.py #   Pre-Norm + Attention + Residual + MLP + Residual
│   └── model.py             #   XingChengModel + XingChengForCausalLM（LM Head + loss）
├── inference/               # 推論層
│   ├── kv_cache.py          #   KV Cache 容器
│   ├── sampler.py           #   greedy / temperature / top-k / top-p / repetition penalty
│   └── generate.py          #   Prefill + Decode 自回歸生成
├── training/                # 訓練層（Autograd）
│   ├── optimizer.py         #   AdamW / SGD 建構
│   ├── data.py              #   TextDataset + collate_batch
│   └── trainer.py           #   訓練迴圈（前向 → backward → optimizer step）
├── quantization/            # 量化層
│   └── quantizer.py         #   weight-only INT8/INT4 + 動態反量化
└── tests/
    └── test_model.py        # 端對端測試：前向 / 反向 / 生成 / 訓練 / 量化 / tokenizer
```

## 技術棧對應

| 層級 | 實作 | 對應檔案 |
|------|------|----------|
| Python 高階介面 | `nn.Module` / Tensor API | `modules/*.py`, `config.py` |
| Tensor Operations | 自研 ops 層（GEMM / Softmax / Reduction / Activation / Gather / Scatter，Triton → PyTorch fallback） | `kernels/tensor_ops.py` |
| 訓練主線（Python + PyTorch） | Autograd、`loss.backward()` + `optimizer.step()` | `training/*.py`, `modules/model.py` |
| 推論 Python 路徑（預設） | PyTorch / Triton 生成（prefill + decode） | `inference/*.py`, `kernels/*.py` |
| 推論 C++ 路徑（部署選項） | 原生 pybind11 延伸 / Gluon / CUDA C++，與 Python 路徑共用權重 | `native/`（A221/E186） |
| ATen / Dispatcher / Torch C++ Backend | PyTorch 內建 | （由 PyTorch 提供） |
| cuBLASLt / cuDNN | `nn.Linear` + `set_gemm_backend` + TF32 / Tensor Core | `execution/backend.py`, `modules/attention.py` |
| FlashAttention | `F.scaled_dot_product_attention`（IO-aware） | `modules/attention.py` |
| Triton 自研 Kernel | RMSNorm / RoPE / SwiGLU / Quant（推論） | `kernels/*.py` |
| PTX / SASS | 由 Triton / CUDA 編譯產出 | （不手寫） |
| CPU 路線 | oneDNN / BLAS via PyTorch | `execution/backend.py` |
| Apple MPS | `torch.device("mps")` 支線 | `execution/backend.py` |

## 後端下沉策略

每個自研運算提供兩層實作，依硬體能力自動選擇：

```
Triton kernel (GPU 首選，IO-aware、operator fusion)
  ↓ Triton 不可用 / 非 CUDA / kernel 編譯失敗
PyTorch 參考實作 (CPU / 無 Triton 環境)  ← 正式保證路徑
```

> **主線裁決（2026-09-19）**：本模型以 Python + PyTorch 寫出完整模型
> （訓練與推論皆是）。C++ 系下沉（`native/` pybind11 / Gluon / CUDA C++ /
> PTX / SASS）**留給後續自研推論引擎**承接——屆時作為推論專屬路徑，
> 與本 Python 路徑共用同一組權重、tokenizer 與語意契約。

Attention 一律優先走 `F.scaled_dot_product_attention`，由 PyTorch 內部調度
FlashAttention / mem-efficient / math 三條路徑；GEMM / Linear 由 PyTorch
依裝置自動調度 cuBLASLt（CUDA + Tensor Core）/ oneDNN（CPU）/ MPS。

**下沉邊界**：逐層下沉僅適用於**推論路徑**（Python 路徑 → C++ 路徑雙軌並存）；
訓練一律維持 Python + PyTorch 主線（Autograd + Optimizer），不因效能因素移出到 C++。

## GPU 加速

以速度與正確性為雙原則（`execution/gpu_acceleration.py` 統一決策，`verify_correctness:1e-3`）：

* **訓練**（Python+PyTorch 主線）：`BF16` autocast（`precision.py:76` `auto→bf16` 無 scaler）、`fused AdamW`（`pretrain.py:165` `cuda` 時 `fused=True`）、`FlashAttention` via `SDPA`（`attention.py:98`）、`batch 8 grad_accum 4` 打包（`pack_blocks`）、`torch.compile` 可選（`PretrainConfig:48` `use_torch_compile`，Windows `PYTHONUTF8` 下自動回退）；實測 `small` `RTX3050 6GB SM8.6` `BF16+Fuse+batch8` `15k tok/s`（`batch2` `10k tok/s`），`6.4×` 於同硬體
* **推論**：
  * Python 路徑（預設）：`FlashAttention` + `KV Cache`（`inference/kv_cache.py`）`prefill/decode` + `Triton`（`kernels/*`）→ `PyTorch` 回退 + `INT8/INT4` weight-only（`quantization/quantizer.py:80` `router` 保持 `FP32`），`small vocab2000` `INT8` `5.0MB` vs `FP 41MB` `diff 0.06` 生成一致
  * C++ 路徑（部署選項）：`native/` pybind11 / Gluon / CUDA C++，與 Python 路徑共用權重，`Native Dispatch` 擇路，`Triton` 不可用時 `has_triton=False` 自動回退

`capabilities:142` / `describe_sdpa_backends:190` 於 `cuda` 實測 `has_tensor_core=True has_cuda=True flash_active=True gemm=cublaslt expected=flash_attention`（`gpu_acceleration.py:32` `plan_for_training/inference`）。

## 預設模型規模

| 預設 | 參數量 | hidden | layers | heads | KV heads | context |
|------|--------|--------|--------|-------|----------|---------|
| `small()`  | ~5M    | 256    | 4      | 8     | 4 (GQA)  | 1,024   |
| `medium()` | ~27M   | 512    | 8      | 8     | 4 (GQA)  | 1,024   |
| `base()`   | ~100M  | 768    | 12     | 12    | 4 (GQA)  | 2,048   |
| `xlarge()` | ~500M  | 1,536  | 18     | 12    | 4 (GQA)  | 4,096   |
| `large()`  | ~1.2B  | 2,048  | 24     | 16    | 4 (GQA)  | 8,192   |

## 快速開始

```python
import torch
from native_transformer import XingChengConfig, XingChengForCausalLM, XingChengTokenizer
from native_transformer.inference import Generator, Sampler, SamplingConfig

# 1. 建立模型（auto 裝置：CUDA > MPS > CPU）
cfg = XingChengConfig.base()
model = XingChengForCausalLM(cfg).to(cfg and "cpu")

# 2. Tokenizer
tok = XingChengTokenizer(vocab_size=cfg.vocab_size)
ids = torch.tensor([tok.encode("星澄原生模型", add_bos=True, add_eos=False)])

# 3. 生成
sampler = Sampler(SamplingConfig(do_sample=True, temperature=0.8, top_k=50, top_p=0.9))
gen = Generator(model, sampler=sampler, device="cpu")
out = gen.generate(ids, max_new_tokens=32)
print(tok.decode(out[0].tolist()))
```

### 訓練

```python
from native_transformer.training import TextDataset, Trainer, TrainingConfig, make_dataloader

ds = TextDataset(["星澄", "本地模型", "transformer"], tok, max_length=64)
loader = make_dataloader(ds, batch_size=2, pad_id=tok.pad_id)
trainer = Trainer(model, TrainingConfig(lr=3e-4, epochs=1, max_steps=50), cfg)
result = trainer.fit(loader)
print("final loss:", result["final_loss"])
```

### 量化

```python
from native_transformer.quantization import quantize_model, dequantize_model
quantize_model(model, n_bits=8)   # weight-only INT8
# ... 推論 ...
dequantize_model(model)           # 還原為 FP32
```

## 執行測試

```powershell
cd E:\GPTBridge\local-model\src\backend\services\xingcheng\infrastructure
python native_transformer\tests\test_model.py -v
```

測試涵蓋：前向 logits 形狀、反向梯度、padding mask、greedy/sampling 生成、
單步訓練 loss、INT8 量化往返、tokenizer 編解碼往返。

## 與既有系統的關係

本套件為星澄的**第一版正式原生模型核心**，與既有模組並存：

- `StarNativeRuntime`：原生唯一生成執行期，所有推論皆路由至本套件權重。
- `StarAutoregressiveLanguageModel`：既有加權 n-gram 安全回退。
- `StarNativeLanguageModel`：既有意圖分類與治理協調層。
- **`native_transformer/`（本套件）**：真正可訓練 / 可推理 / 可量化的
  PyTorch Transformer 原生模型，逐步取代外部依賴，實現「完全本地執行」目標。

## 未來路線

主線（本模型核心，Python + PyTorch）：

1. **Triton kernel 完整化**：RoPE / KV Cache / Sampling 的逐元素 Triton kernel
   （PyTorch 生態內選項，有 CUDA+Triton 環境才啟用）。
2. **進階量化**：per-channel / group-wise / AWQ / GPTQ / FP8。
3. **Apple MPS / Metal**：未來支線。
4. **JAX + XLA**：研究 / 架構實驗支線，不納入第一版正式核心。
5. **TensorFlow**：僅保留為相容 / 研究選項，不與 PyTorch 主架構同時作為正式依賴。

後續自研推論引擎（C++ 範疇，承接時與 Python 路徑共用權重、tokenizer 與
語意契約；訓練永不涉 C++）：

- **Gluon**：Triton 無法提供足夠硬體控制（Tensor Layout / Shared Memory /
  Warp / Data Movement）時的下沉選項。
- **CUDA C++**：高吞吐 / 低延遲部署或硬體特化需求，走 `native/` 標準樹
  （A221/E186）pybind11 延伸。
- **PTX / SASS**：極端底層最佳化 / 效能分析 / 硬體指令研究，不手寫。
