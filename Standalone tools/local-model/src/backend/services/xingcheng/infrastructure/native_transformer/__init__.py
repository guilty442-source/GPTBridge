"""星澄原生模型 (XingCheng Native Model) — 本地原生 Transformer 架構。

訓練退役（B167/B38）：JAX/XLA 與 PyTorch 已全數退役——零 source、
dependency、artifact、execution 與 fallback 角色，無過渡期。
本套件保留推論面（tokenizer / bpe / checkpoint / modules / inference）；
jax_backend、training/、self_learning、maturity 已移除。

    星澄
      → Transformer (Embedding / Attention / MLP / RMSNorm / Residual / LM Head / Sampling)
      → Tensor Operations (GEMM / Softmax / Reduction / Activation / Gather / Scatter ...)
      → CPU / NVIDIA GPU

載入行為：本套件符號一律惰性解析——import 本套件或其子模組不載入
任何外部數值框架；缺少相關 lineage 依賴時 fail-closed。
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
