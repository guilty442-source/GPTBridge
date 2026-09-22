"""Multi-Head / Grouped-Query Attention。

對應技術棧：
  - Q/K/V projection：cuBLASLt (CUDA) / oneDNN (CPU) via nn.Linear
  - RoPE：自研 kernel（CUDA Triton → CPU 原生 C → PyTorch）
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
from ..execution.dispatch import native_attention
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
        b, kv_heads, s, d = x.shape
        return (
            x[:, :, None, :, :]
            .expand(b, kv_heads, n_rep, s, d)
            .reshape(b, kv_heads * n_rep, s, d)
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

        # KV Cache 接續（回傳值只含本步新增的 K/V）
        new_kv = (k, v) if use_cache else None
        if use_cache and kv_cache is not None:
            past_k, past_v = kv_cache
            # 快取可能以 fp16/bf16/int8 儲存；拼接前對齊激活 dtype。
            if past_k.dtype != k.dtype:
                past_k = past_k.to(k.dtype)
            if past_v.dtype != v.dtype:
                past_v = past_v.to(v.dtype)
            k = torch.cat([past_k, k], dim=2)
            v = torch.cat([past_v, v], dim=2)

        # GQA：優先讓 SDPA 原生處理 head ratio，避免物化重複 KV。
        n_rep = self.num_heads // self.num_kv_heads
        use_native_gqa = n_rep > 1

        # Attention via SDPA（自動調度 FlashAttention / mem-efficient / math）
        # 有 attention_mask 時因果限制已內含於 mask；無 mask 時僅 q/k 等長可用內建 causal
        is_causal = attention_mask is None and q.size(2) == k.size(2)
        attn = dispatch_attention(
            q, k, v,
            dropout_p=self.dropout if self.training else 0.0,
            is_causal=is_causal,
            attention_mask=attention_mask,
            scaling=self.scaling,
            enable_gqa=use_native_gqa,
        )
        attn = attn.transpose(1, 2).contiguous().view(b, s, self.num_heads * self.head_dim)
        return self.o_proj(attn), new_kv


def dispatch_attention(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    *,
    dropout_p: float,
    is_causal: bool,
    attention_mask: torch.Tensor | None,
    scaling: float,
    enable_gqa: bool = False,
) -> torch.Tensor:
    """統一入口：Native Dispatch（原生 C 計算核心）→ SDPA → 手動 softmax。

    依 A219，原生核心不可用、未達門檻或 parity 未通過時一律回退
    PyTorch 路徑；推論結果在容差內等價。
    """
    if dropout_p <= 0 and not enable_gqa:
        native = native_attention(
            q, k, v,
            scale=scaling,
            is_causal=is_causal,
            attention_mask=attention_mask,
        )
        if native is not None:
            return native
    return _sdpa_attention(
        q, k, v,
        dropout_p=dropout_p,
        is_causal=is_causal,
        attention_mask=attention_mask,
        scaling=scaling,
        enable_gqa=enable_gqa,
    )


def _sdpa_attention(
    q: torch.Tensor,
    k: torch.Tensor,
    v: torch.Tensor,
    *,
    dropout_p: float,
    is_causal: bool,
    attention_mask: torch.Tensor | None,
    scaling: float,
    enable_gqa: bool = False,
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
                enable_gqa=enable_gqa,
            )
        except Exception:
            if enable_gqa:
                n_rep = q.size(1) // k.size(1)
                k = k.repeat_interleave(n_rep, dim=1)
                v = v.repeat_interleave(n_rep, dim=1)
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
    if enable_gqa and q.size(1) != k.size(1):
        n_rep = q.size(1) // k.size(1)
        k = k.repeat_interleave(n_rep, dim=1)
        v = v.repeat_interleave(n_rep, dim=1)
    attn_weights = torch.matmul(q, k.transpose(-1, -2)) * scaling
    if attention_mask is not None:
        attn_weights = attn_weights + attention_mask
    if is_causal and attention_mask is None:
        q_len, k_len = q.size(-2), k.size(-1)
        offset = k_len - q_len
        keys = torch.arange(k_len, device=q.device).unsqueeze(0)
        queries = torch.arange(q_len, device=q.device).unsqueeze(1) + offset
        blocked = keys > queries
        causal = torch.zeros(q_len, k_len, device=q.device, dtype=attn_weights.dtype)
        causal = causal.masked_fill(blocked, float("-inf"))
        attn_weights = attn_weights + causal
    attn_weights = torch.softmax(attn_weights, dim=-1)
    if dropout_p > 0:
        attn_weights = torch.dropout(attn_weights, dropout_p, train=True)
    return torch.matmul(attn_weights, v)


__all__ = ["XingChengAttention", "dispatch_attention"]
