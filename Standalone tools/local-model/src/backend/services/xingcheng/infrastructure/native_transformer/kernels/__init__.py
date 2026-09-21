"""星澄自研 GPU Kernel 層。

優先順序：
  Triton → PyTorch（正式保證路徑）

本套件為每個自研運算提供兩層實作：
  1. Triton kernel（GPU 首選，IO-aware、operator fusion）
  2. PyTorch 參考實作（CPU / 無 Triton 環境的 fallback）

當 Triton 不可用或裝置非 CUDA 時，自動退回 PyTorch 實作，
確保星澄在純 CPU 環境仍可完全本地執行。

主線裁決（2026-09-19）：模型以 Python + PyTorch 完整實作；
Gluon / CUDA C++ / PTX 等 C++ 系下沉留給後續自研推論引擎，
本套件不預留 C++ 介面。
"""

from __future__ import annotations

from .rmsnorm import rms_norm, rms_norm_weight
from .rope import apply_rope, build_rope_tables
from .swiglu import swiglu
from .quant import (
    quantize_per_tensor,
    dequantize_per_tensor,
    pack_int4,
    unpack_int4,
)
from .tensor_ops import (
    activation,
    gather,
    gemm,
    reduce_max,
    reduce_mean,
    reduce_sum,
    scatter_add,
    softmax,
)

__all__ = [
    "rms_norm",
    "rms_norm_weight",
    "apply_rope",
    "build_rope_tables",
    "swiglu",
    "quantize_per_tensor",
    "dequantize_per_tensor",
    "pack_int4",
    "unpack_int4",
    "activation",
    "gather",
    "gemm",
    "reduce_max",
    "reduce_mean",
    "reduce_sum",
    "scatter_add",
    "softmax",
]
