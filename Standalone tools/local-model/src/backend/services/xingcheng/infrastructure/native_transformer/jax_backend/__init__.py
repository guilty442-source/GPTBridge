"""星澄 JAX 訓練後端 — A612 唯一訓練框架（JAX+XLA）。

PyTorch 已退役（migration-only lineage during the A610 window）；新的
訓練研究路徑走本套件。dense decoder 覆蓋 XingChengConfig 主線
（RMSNorm + RoPE + GQA + SwiGLU + tied LM head）；MoE/量化配置屬
migration-only，JAX 路徑宣告 ``JAX_BACKEND_UNSUPPORTED`` fail-closed。
"""

from __future__ import annotations

from .checkpoint import load_jax_checkpoint, save_jax_checkpoint
from .model import forward_logits, init_params
from .sft import JaxSFTConfig, jax_sft_train

__all__ = [
    "JaxSFTConfig",
    "forward_logits",
    "init_params",
    "jax_sft_train",
    "load_jax_checkpoint",
    "save_jax_checkpoint",
]
