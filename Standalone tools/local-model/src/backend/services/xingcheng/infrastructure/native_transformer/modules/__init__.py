"""星澄 Transformer 核心模組。

包含：
  - Embedding（token + 選用 learned position）
  - RMSNorm / LayerNorm（透過 kernels 統一呼叫）
  - RoPE
  - Multi-Head / Grouped-Query Attention（FlashAttention via SDPA）
  - MLP / FFN（SwiGLU）
  - Transformer Block（residual connection）
  - LM Head
"""

from __future__ import annotations

from .embedding import XingChengEmbeddings
from .norm import XingChengNorm
from .attention import XingChengAttention
from .mlp import XingChengMLP
from .transformer_block import XingChengBlock
from .model import XingChengModel, XingChengForCausalLM

__all__ = [
    "XingChengEmbeddings",
    "XingChengNorm",
    "XingChengAttention",
    "XingChengMLP",
    "XingChengBlock",
    "XingChengModel",
    "XingChengForCausalLM",
]
