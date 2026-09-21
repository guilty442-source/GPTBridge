"""Transformer Block：Pre-Norm + Attention + Residual + MLP + Residual。"""

from __future__ import annotations

import torch
import torch.nn as nn

from ..config import XingChengConfig
from .attention import XingChengAttention
from .mlp import XingChengMLP
from .norm import XingChengNorm

try:
    from .moe import XingChengMoE
except ImportError:
    XingChengMoE = None  # type: ignore


class XingChengBlock(nn.Module):
    """單層 Transformer 解碼器區塊（Pre-Norm 殘差結構）。"""

    def __init__(self, config: XingChengConfig, layer_idx: int = 0) -> None:
        super().__init__()
        self.config = config
        self.layer_idx = int(layer_idx)
        self.input_norm = XingChengNorm(config)
        self.attention = XingChengAttention(config)
        self.post_attention_norm = XingChengNorm(config)
        # MoE 判定：use_moe 且層索引符合間隔
        use_moe_layer = bool(config.use_moe and XingChengMoE is not None and (self.layer_idx % max(1, config.moe_layer_interval) == 0))
        if use_moe_layer:
            self.mlp = XingChengMoE(config)
            self.is_moe = True
        else:
            self.mlp = XingChengMLP(config)
            self.is_moe = False

    def _forward_impl(
        self,
        hidden_states: torch.Tensor,
        cos: torch.Tensor | None,
        sin: torch.Tensor | None,
        position_ids: torch.Tensor | None,
        attention_mask: torch.Tensor | None,
        kv_cache: tuple[torch.Tensor, torch.Tensor] | None,
        use_cache: bool,
    ) -> tuple[torch.Tensor, tuple[torch.Tensor, torch.Tensor] | None, torch.Tensor | None]:
        # Attention 殘差
        normed = self.input_norm(hidden_states)
        attn_out, new_kv = self.attention(
            normed,
            cos=cos, sin=sin, position_ids=position_ids,
            attention_mask=attention_mask,
            kv_cache=kv_cache, use_cache=use_cache,
        )
        hidden_states = hidden_states + attn_out
        # MLP / MoE 殘差
        normed = self.post_attention_norm(hidden_states)
        aux_loss: torch.Tensor | None = None
        if getattr(self, "is_moe", False):
            mlp_out, aux_loss = self.mlp(normed)  # type: ignore[misc]
        else:
            mlp_out = self.mlp(normed)
        hidden_states = hidden_states + mlp_out
        return hidden_states, new_kv, aux_loss

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
    ) -> tuple[torch.Tensor, tuple[torch.Tensor, torch.Tensor] | None, torch.Tensor | None]:
        # VRAM 優化：activation checkpoint 僅在訓練且無 KV cache 時啟用（重計算省 30-50%）
        if getattr(self.config, "use_activation_checkpoint", False) and self.training and not use_cache:
            # use_reentrant=False 為新版推薦，避免在 compiled 模型下警告
            return torch.utils.checkpoint.checkpoint(
                self._forward_impl,
                hidden_states, cos, sin, position_ids, attention_mask, kv_cache, use_cache,
                use_reentrant=False,
            )
        return self._forward_impl(hidden_states, cos, sin, position_ids, attention_mask, kv_cache, use_cache)


__all__ = ["XingChengBlock"]
