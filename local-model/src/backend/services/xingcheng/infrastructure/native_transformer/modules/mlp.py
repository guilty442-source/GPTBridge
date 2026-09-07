"""MLP / FFN：SwiGLU 或標準 GeLU/SiLU FFN。

對應技術棧：
  - Linear projection：cuBLASLt (CUDA) / oneDNN (CPU) via nn.Linear
  - SwiGLU gate：自研 kernel（Triton → PyTorch）
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from ..config import XingChengConfig
from ..kernels import swiglu


class XingChengMLP(nn.Module):
    """FFN：SwiGLU 預設，否則標準 two-layer MLP。"""

    def __init__(self, config: XingChengConfig) -> None:
        super().__init__()
        self.config = config
        self.use_swiglu = config.use_swiglu
        if config.use_swiglu:
            self.gate_proj = nn.Linear(config.hidden_size, config.intermediate_size, bias=False)
            self.up_proj = nn.Linear(config.hidden_size, config.intermediate_size, bias=False)
            self.down_proj = nn.Linear(config.intermediate_size, config.hidden_size, bias=False)
        else:
            self.fc1 = nn.Linear(config.hidden_size, config.intermediate_size)
            self.fc2 = nn.Linear(config.intermediate_size, config.hidden_size)
        self._init_weights()

    def _init_weights(self) -> None:
        if self.use_swiglu:
            for proj in (self.gate_proj, self.up_proj, self.down_proj):
                nn.init.normal_(proj.weight, mean=0.0, std=self.config.initializer_range)
        else:
            nn.init.normal_(self.fc1.weight, mean=0.0, std=self.config.initializer_range)
            nn.init.zeros_(self.fc1.bias)
            nn.init.normal_(self.fc2.weight, mean=0.0, std=self.config.initializer_range)
            nn.init.zeros_(self.fc2.bias)

    def forward(self, hidden_states: torch.Tensor) -> torch.Tensor:
        if self.use_swiglu:
            gate = self.gate_proj(hidden_states)
            up = self.up_proj(hidden_states)
            return self.down_proj(swiglu(gate, up))
        h = self.fc1(hidden_states)
        if self.config.hidden_act == "silu":
            h = F.silu(h)
        elif self.config.hidden_act == "gelu":
            h = F.gelu(h)
        else:
            h = F.relu(h)
        return self.fc2(h)


__all__ = ["XingChengMLP"]
