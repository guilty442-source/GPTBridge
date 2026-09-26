"""星澄原生模型 (XingCheng Native Model) — 本地原生 Transformer 架構。

正式主線技術棧（A612：JAX+XLA 為唯一訓練框架；PyTorch 為 migration-only lineage）：

    星澄
      → Python
      → JAX/XLA（jax_backend；訓練）+ PyTorch lineage（遷移窗口，唯讀延續）
      → Transformer (Embedding / Attention / MLP / RMSNorm / Residual / LM Head / Sampling)
      → Tensor Operations (GEMM / Softmax / Reduction / Activation / Gather / Scatter ...)
      → Computation Graph + Autograd (Backward Graph / Gradient / Optimizer)
      → 高效能數學與 Kernel (BLAS / oneDNN / cuBLASLt / cuDNN / FlashAttention)
      → CPU / NVIDIA GPU (Apple MPS 為未來支線)

    （Gluon / CUDA C++ / PTX / SASS 屬後續自研推論引擎範疇，
     不在本模型核心範圍 — 主線裁決 2026-09-19）

設計原則：
  1. 優先使用成熟高效函式庫，不重複造輪子。
  2. 訓練主線為 JAX/XLA（jax_backend）；PyTorch 實作保留為 A610
     python-reduction 窗口內的 migration-only lineage，不為新工作宣告。
  3. Python 負責模型設計與高階控制；JAX/XLA 負責 Tensor / Autograd /
     執行框架；高效能數學運算由 XLA 內部調度。

本套件為正式核心，提供可完全本地執行、可訓練、可推理、可量化、
可自訂 Kernel 的原生模型實作；C++ 層效能下沉留給後續自研推論引擎。

載入行為（A612）：torch lineage 符號（checkpoint / model / tokenizer）
一律惰性解析——import 本套件或其 torch-free 子模組（config、
jax_backend、bpe）不需要 PyTorch；只有在實際存取 torch 系符號時才
載入 torch，環境缺 torch 時 fail-closed。
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY: dict[str, tuple[str, str]] = {
    "NativeBPETokenizer": (".bpe", "NativeBPETokenizer"),
    "train_bpe": (".bpe", "train_bpe"),
    "FORMAT_VERSION": (".checkpoint", "FORMAT_VERSION"),
    "load_checkpoint": (".checkpoint", "load_checkpoint"),
    "save_checkpoint": (".checkpoint", "save_checkpoint"),
    "XingChengConfig": (".config", "XingChengConfig"),
    "XingChengForCausalLM": (".modules.model", "XingChengForCausalLM"),
    "XingChengTokenizer": (".tokenizer", "XingChengTokenizer"),
}


def __getattr__(name: str) -> Any:
    target = _LAZY.get(name)
    if target is None:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        )
    module = importlib.import_module(target[0], __name__)
    value = getattr(module, target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(__all__) | set(globals()))


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
