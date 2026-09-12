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
      → Triton / Gluon / CUDA (自研 GPU Kernel)
      → PTX / SASS (極端底層最佳化)
      → CPU / NVIDIA GPU (Apple MPS 為未來支線)

設計原則：
  1. 優先使用成熟高效函式庫，不重複造輪子。
  2. 只有實際效能瓶頸時才逐層下沉。
  3. Python 負責模型設計與高階控制；PyTorch 負責 Tensor / Autograd / 執行框架；
     ATen / C++ 負責底層 Tensor 與 Runtime；cuBLASLt / cuDNN / FlashAttention
     負責成熟高效數學運算；Triton / Gluon 負責星澄自研 GPU Kernel；
     CUDA / PTX 負責極低階 NVIDIA GPU 最佳化。

本套件為第一版正式核心，提供可完全本地執行、可訓練、可推理、可量化、
可自訂 Kernel、可逐層下沉硬體級最佳化的原生模型實作。
"""

from __future__ import annotations

from .config import XingChengConfig
from .modules.model import XingChengForCausalLM
from .tokenizer import XingChengTokenizer

__all__ = [
    "XingChengConfig",
    "XingChengForCausalLM",
    "XingChengTokenizer",
    "__version__",
]

__version__ = "1.00000"
