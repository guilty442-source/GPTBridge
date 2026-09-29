"""星澄原生模型 (XingCheng Native Model) — 本地原生推論套件。

訓練與 Python 推論退役（B167/B38/E180）：JAX/XLA 與 PyTorch 已全數
退役——零 source、dependency、artifact、execution 與 fallback 角色，
無過渡期。``jax_backend``、``training/*.py``、``self_learning``、
``maturity`` 與 PyTorch 推論棧（``modules`` / ``kernels`` /
``inference`` / ``execution`` / ``quantization`` / ``checkpoint`` /
``cpp_export`` / ``benchmark`` / ``capability_eval``）皆已移除；
模型執行只由正式 C++ 推論引擎（``cpp_runtime`` →
``_xingcheng_inference``）服務，權重為已驗證
``star-native-inference-bundle/v1`` 匯出物，原生訓練管線為
``training/xingcheng_trainer.exe``（C++）＋ ``xct-executor``（.NET）。

本套件保留語言層推論周邊（tokenizer / bpe / chat_format / config /
lifecycle / retention）與 C++ 引擎路由（cpp_runtime）。

載入行為：本套件符號一律惰性解析——import 本套件或其子模組不載入
任何外部數值框架；缺少相關 lineage 依賴時 fail-closed。
"""

from __future__ import annotations

import importlib
from typing import Any

_LAZY: dict[str, tuple[str, str]] = {
    "NativeBPETokenizer": (".bpe", "NativeBPETokenizer"),
    "train_bpe": (".bpe", "train_bpe"),
    "XingChengConfig": (".config", "XingChengConfig"),
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
    "NativeBPETokenizer",
    "XingChengConfig",
    "XingChengTokenizer",
    "train_bpe",
    "__version__",
]

__version__ = "1.00000"
