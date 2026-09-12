# 星澄原生模型 (XingCheng Native Model)

「星澄」本地原生 AI 模型架構 — 高效能、可擴充、可逐層下沉最佳化的
PyTorch Transformer 原生實作。

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
2. **只有實際效能瓶頸時才逐層下沉。**
3. Python 負責模型設計與高階控制；PyTorch 負責 Tensor / Autograd / 執行框架；
   ATen / C++ 負責底層 Tensor 與 Runtime；cuBLASLt / cuDNN / FlashAttention
   負責成熟高效數學運算；Triton / Gluon 負責星澄自研 GPU Kernel；
   CUDA / PTX 負責極低階 NVIDIA GPU 最佳化。

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
│   └── quant.py             #   Quantization / Dequantization（Triton kernel + PyTorch 參考）
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
| Tensor Operations | PyTorch Tensor ops（GEMM / Softmax / Reduction / Activation） | `modules/attention.py`, `modules/mlp.py` |
| Computation Graph + Autograd | `loss.backward()` + `optimizer.step()` | `training/trainer.py`, `modules/model.py` |
| ATen / Dispatcher / Torch C++ Backend | PyTorch 內建 | （由 PyTorch 提供） |
| cuBLASLt / cuDNN | `nn.Linear` + `set_gemm_backend` + TF32 / Tensor Core | `execution/backend.py`, `modules/attention.py` |
| FlashAttention | `F.scaled_dot_product_attention`（IO-aware） | `modules/attention.py` |
| Triton 自研 Kernel | RMSNorm / RoPE / SwiGLU / Quant | `kernels/*.py` |
| Gluon / CUDA C++ | 預留介面（極端瓶頸下沉） | `kernels/*.py`（fallback 鏈） |
| PTX / SASS | 由 Triton / CUDA 編譯產出 | （不手寫） |
| CPU 路線 | oneDNN / BLAS via PyTorch | `execution/backend.py` |
| Apple MPS | `torch.device("mps")` 支線 | `execution/backend.py` |

## 後端下沉策略

每個自研運算提供三層實作，依硬體能力自動選擇：

```
Triton kernel (GPU 首選，IO-aware、operator fusion)
  ↓ Triton 不可用 / 非 CUDA / kernel 編譯失敗
PyTorch 參考實作 (CPU / 無 Triton 環境)
  ↓ 極端效能瓶頸
Gluon / CUDA C++ (預留，未來下沉)
```

Attention 一律優先走 `F.scaled_dot_product_attention`，由 PyTorch 內部調度
FlashAttention / mem-efficient / math 三條路徑；GEMM / Linear 由 PyTorch
依裝置自動調度 cuBLASLt（CUDA + Tensor Core）/ oneDNN（CPU）/ MPS。

## 預設模型規模

| 預設 | 參數量 | hidden | layers | heads | KV heads | context |
|------|--------|--------|--------|-------|----------|---------|
| `small()`  | ~5M    | 256    | 4      | 8     | 4 (GQA)  | 1,024   |
| `base()`   | ~100M  | 768    | 12     | 12    | 4 (GQA)  | 2,048   |
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

- `StarTransformerRuntime`：既有 Ollama 迴圈介接器（外部基礎權重）。
- `StarAutoregressiveLanguageModel`：既有加權 n-gram 安全回退。
- `StarNativeLanguageModel`：既有意圖分類與治理協調層。
- **`native_transformer/`（本套件）**：真正可訓練 / 可推理 / 可量化的
  PyTorch Transformer 原生模型，逐步取代外部依賴，實現「完全本地執行」目標。

## 未來下沉路線

1. **Triton kernel 完整化**：RoPE / KV Cache / Sampling 的逐元素 Triton kernel。
2. **Gluon**：當 Triton 無法提供足夠硬體控制（Tensor Layout / Shared Memory /
   Warp / Data Movement）時下沉。
3. **CUDA C++**：極端效能瓶頸或硬體特化需求時下沉，透過專案根目錄
   `native/` 標準樹（A221/E186）的 pybind11 延伸編譯。
4. **PTX / SASS**：僅用於極端底層最佳化、效能分析或硬體指令研究，不手寫。
5. **進階量化**：per-channel / group-wise / AWQ / GPTQ / FP8。
6. **Apple MPS / Metal**：未來支線。
7. **JAX + XLA**：研究 / 架構實驗支線，不納入第一版正式核心。
8. **TensorFlow**：僅保留為相容 / 研究選項，不與 PyTorch 主架構同時作為正式依賴。
