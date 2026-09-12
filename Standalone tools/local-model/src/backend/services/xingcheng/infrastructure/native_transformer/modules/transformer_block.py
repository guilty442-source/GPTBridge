"""Transformer Block：Pre-Norm + Attention + Residual + MLP + Residual。"""

from __future__ import annotations

import torch
import torch.nn as nn

from ..config import XingChengConfig
from .attention import XingChengAttention
from .mlp import XingChengMLP
from .norm import XingChengNorm


class XingChengBlock(nn.Module):
    """單層 Transformer 解碼器區塊（Pre-Norm 殘差結構）。"""

    def __init__(self, config: XingChengConfig) -> None:
        super().__init__()
        self.config = config
        self.input_norm = XingChengNorm(config)
        self.attention = XingChengAttention(config)
        self.post_attention_norm = XingChengNorm(config)
        self.mlp = XingChengMLP(config)

    def forward(
        self,
        hidden_states: torch.Tensor,
        *,
        cos: torch.Tensor | None = None,
        sin: torch.Tensor | None = None,
        position_ids: torch.Tensor | None = None,
        attention_mask: torch.Tensor | None = None,
        kv_cache: tuple[torch.Tensor, torch.Tensor] | None = None,
        use_cache: bool = False,
    ) -> tuple[torch.Tensor, tuple[torch.Tensor, torch.Tensor] | None]:
        # Attention 殘差
        normed = self.input_norm(hidden_states)
        attn_out, new_kv = self.attention(
            normed,
            cos=cos, sin=sin, position_ids=position_ids,
            attention_mask=attention_mask,
            kv_cache=kv_cache, use_cache=use_cache,
        )
        hidden_states = hidden_states + attn_out

        # MLP 殘差
        normed = self.post_attention_norm(hidden_states)
        mlp_out = self.mlp(normed)
        hidden_states = hidden_states + mlp_out
        return hidden_states, new_kv


__all__ = ["XingChengBlock"]
