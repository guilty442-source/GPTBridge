"""Normalization 層：RMSNorm / LayerNorm 統一介面。

RMSNorm 走自研 kernel（Triton → PyTorch），
LayerNorm 直接使用 PyTorch 內建（背後走 cuDNN / oneDNN）。
"""

from __future__ import annotations

import torch
import torch.nn as nn

from ..config import XingChengConfig
from ..kernels import rms_norm as rms_norm_kernel


class XingChengNorm(nn.Module):
    """依 config.norm_type 切換 RMSNorm / LayerNorm。"""

    def __init__(self, config: XingChengConfig, hidden_size: int | None = None) -> None:
        super().__init__()
        self.config = config
        self.hidden_size = hidden_size or config.hidden_size
        self.weight = nn.Parameter(torch.ones(self.hidden_size))
        if config.norm_type == "rmsnorm":
            self.eps = config.rms_norm_eps
            self._layer_norm = None
        elif config.norm_type == "layernorm":
            self.eps = config.layer_norm_eps
            self._layer_norm = nn.LayerNorm(
                self.hidden_size, eps=config.layer_norm_eps, elementwise_affine=False
            )
        else:
            raise ValueError(f"未知 norm_type: {config.norm_type}")

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        if self._layer_norm is not None:
            return self._layer_norm(hidden_states) * self.weight
        return rms_norm_kernel(hidden_states, self.weight, self.eps)


__all__ = ["XingChengNorm"]
