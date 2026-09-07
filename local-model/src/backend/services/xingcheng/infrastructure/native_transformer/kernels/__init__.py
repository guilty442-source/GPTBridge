"""星澄自研 GPU Kernel 層。

優先順序：
  Triton → Gluon → CUDA C++

本套件為每個自研運算提供三層實作：
  1. Triton kernel（GPU 首選，IO-aware、operator fusion）
  2. PyTorch 參考實作（CPU / 無 Triton 環境的 fallback）
  3. 預留 Gluon / CUDA C++ 介面（極端瓶頸時下沉）

當 Triton 不可用或裝置非 CUDA 時，自動退回 PyTorch 實作，
確保星澄在純 CPU 環境仍可完全本地執行。
"""

from __future__ import annotations

from .rmsnorm import rms_norm, rms_norm_weight
from .rope import apply_rope, build_rope_tables
from .swiglu import swiglu
from .quant import quantize_per_tensor, dequantize_per_tensor

__all__ = [
    "rms_norm",
    "rms_norm_weight",
    "apply_rope",
    "build_rope_tables",
    "swiglu",
    "quantize_per_tensor",
    "dequantize_per_tensor",
]
