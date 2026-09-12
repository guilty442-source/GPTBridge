"""Multi-Head / Grouped-Query Attention。

對應技術棧：
  - Q/K/V projection：cuBLASLt (CUDA) / oneDNN (CPU) via nn.Linear
  - RoPE：自研 kernel（Triton → PyTorch）
  - Attention score：FlashAttention via torch SDPA（IO-aware，降低 VRAM 存取）
  - Output projection：cuBLASLt / oneDNN

支援 GQA / MQA（num_key_value_heads < num_attention_heads），
KV Cache 由 inference 層管理，本模組負責單次前向。
"""

from __future__ import annotations

import torch
import torch.nn as nn
import torch.nn.functional as F

from ..config import XingChengConfig
from ..execution.backend import capabilities
from ..kernels import apply_rope


class XingChengAttention(nn.Module):
    """Self-Attention with RoPE + FlashAttention (SDPA) + GQA。"""

    def __init__(self, config: XingChengConfig) -> None:
        super().__init__()
        self.config = config
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.num_kv_heads = config.num_key_value_heads
        self.head_dim = config.head_dim
        self.kv_dim = config.kv_dim
        self.scaling = self.head_dim ** -0.5
        bias = config.attention_bias

        self.q_proj = nn.Linear(self.hidden_size, self.num_heads * self.head_dim, bias=bias)
        self.k_proj = nn.Linear(self.hidden_size, self.kv_dim, bias=bias)
        self.v_proj = nn.Linear(self.hidden_size, self.kv_dim, bias=bias)
        self.o_proj = nn.Linear(self.num_heads * self.head_dim, self.hidden_size, bias=bias)
        self.dropout = config.attention_dropout
        self._init_weights()

    def _init_weights(self) -> None:
        for proj in (self.q_proj, self.k_proj, self.v_proj, self.o_proj):
            nn.init.normal_(proj.weight, mean=0.0, std=self.config.initializer_range)
            if proj.bias is not None:
                nn.init.zeros_(proj.bias)

    # ── GQA reshape ─────────────────────────────────────────────
    def _repeat_kv(self, x: torch.Tensor, n_rep: int) -> torch.Tensor:
        if n_rep == 1:
            return x
        b, _, s, d = x.shape
        return x[:, :, :, None, :].expand(b, self.num_kv_heads, s, n_rep, d).reshape(
            b, self.num_kv_heads * n_rep, s, d
        )

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
        b, s, _ = hidden_states.shape

        q = self.q_proj(hidden_states).view(b, s, self.num_heads, self.head_dim).transpose(1, 2)
        k = self.k_proj(hidden_states).view(b, s, self.num_kv_heads, self.head_dim).transpose(1, 2)
        v = self.v_proj(hidden_states).view(b, s, self.num_kv_heads, self.head_dim).transpose(1, 2)

        # RoPE
        if cos is not None and sin is not None:
            q, k = apply_rope(q, k, cos, sin, position_ids=position_ids)

        # KV Cache 接續
        if use_cache and kv_cache is not None:
            past_k, past_v = kv_cache
            k = torch.cat([past_k, k], dim=2)
            v = torch.cat([past_v, v], dim=2)
        new_kv = (k, v) if use_cache else None

        # GQA：將 KV 重複到與 Q 相同頭數
        n_rep = self.num_heads // self.num_kv_heads
        k_rep = self._repeat_kv(k, n_rep)
        v_rep = self._repeat_kv(v, n_rep)

        # Attention via SDPA（自動調度 FlashAttention / mem-efficient / math）
        attn = _sdpa_attention(
            q, k_rep, v_rep,
            dropout_p=self.dropout if self.training else 0.0,
            is_causal=attention_mask is None,  # 無 mask 時用內建 causal
            attention_mask=attention_mask,
            scaling=self.scaling,
        )
        attn = attn.transpose(1, 2).contiguous().view(b, s, self.num_heads * self.head_dim)
        return self.o_proj(attn), new_kv


def _sdpa_attention(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    *,
    dropout_p: float,
    is_causal: bool,
    attention_mask: torch.Tensor | None,
    scaling: float,
) -> torch.Tensor:
    """統一入口：優先 FlashAttention via SDPA，否則手動 softmax。"""
    cap = capabilities()
    use_sdpa = cap.has_flash_attention
    if use_sdpa:
        try:
            return F.scaled_dot_product_attention(
                q, k, v,
                attn_mask=attention_mask,
                dropout_p=dropout_p if dropout_p > 0 else 0.0,
                is_causal=is_causal and attention_mask is None,
                scale=scaling,
            )
        except Exception:
            pass
    # 手動 fallback（CPU / 舊 PyTorch）
    attn_weights = torch.matmul(q, k.transpose(-1, -2)) * scaling
    if attention_mask is not None:
        attn_weights = attn_weights + attention_mask
    if is_causal and attention_mask is None:
        s = q.size(-2)
        causal = torch.triu(
            torch.full((s, s), float("-inf"), device=q.device, dtype=attn_weights.dtype), diagonal=1
        )
        attn_weights = attn_weights + causal
    attn_weights = torch.softmax(attn_weights, dim=-1)
    if dropout_p > 0:
        attn_weights = torch.dropout(attn_weights, dropout_p, train=True)
    return torch.matmul(attn_weights, v)


__all__ = ["XingChengAttention"]
