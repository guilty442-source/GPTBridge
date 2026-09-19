"""星澄原生模型 (XingCheng Native Model) — 本地原生 Transformer 架構。

正式主線技術棧：

    星澄
      → Python
      → PyTorch (nn.Module / Tensor / Autograd)
      → Transformer (Embedding / Attention / MLP / RMSNorm / Residual / LM Head / Sampling)
      → Tensor Operations (GEMM / Softmax / Reduction / Activation / Gather / Scatter ...)
      → Computation Graph + Autograd (Backward Graph / Gradient / Optimizer)
      → ATen / Dispatcher / Torch C++ Backend
      → 高效能數學與 Kernel (BLAS / oneDNN / cuBLASLt / cuDNN / FlashAttention)
      → Triton (自研 GPU Kernel，PyTorch 生態內選項)
      → CPU / NVIDIA GPU (Apple MPS 為未來支線)

    （Gluon / CUDA C++ / PTX / SASS 屬後續自研推論引擎範疇，
     不在本模型核心範圍 — 主線裁決 2026-09-19）

設計原則：
  1. 優先使用成熟高效函式庫，不重複造輪子。
  2. 完整模型以 Python + PyTorch 實作（訓練與推論皆然）；只有實際效能瓶頸
     時才在 PyTorch 生態內下沉（Triton kernel）。
  3. Python 負責模型設計與高階控制；PyTorch 負責 Tensor / Autograd / 執行框架；
     ATen / C++ Backend 與 cuBLASLt / cuDNN / FlashAttention 由 PyTorch 內部
     調度提供成熟高效數學運算；Triton 為自研 GPU Kernel 選項。

本套件為第一版正式核心，提供可完全本地執行、可訓練、可推理、可量化、
可自訂 Kernel 的原生模型實作；C++ 層效能下沉留給後續自研推論引擎。
"""

from __future__ import annotations

from .bpe import NativeBPETokenizer, train_bpe
from .checkpoint import FORMAT_VERSION, load_checkpoint, save_checkpoint
from .config import XingChengConfig
from .modules.model import XingChengForCausalLM
from .tokenizer import XingChengTokenizer

__all__ = [
    "FORMAT_VERSION",
    "NativeBPETokenizer",
    "XingChengConfig",
    "XingChengForCausalLM",
    "XingChengTokenizer",
    "load_checkpoint",
    "save_checkpoint",
    "train_bpe",
    "__version__",
]

__version__ = "1.00000"
