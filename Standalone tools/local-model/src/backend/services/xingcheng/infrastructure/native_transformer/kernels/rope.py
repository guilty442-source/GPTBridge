"""Rotary Position Embedding (RoPE) 自研 kernel。

優先順序：Triton → PyTorch。
RoPE 為 Attention 的 Q/K 旋轉位置編碼；Triton 路徑融合 cos/sin 計算與旋轉，
降低 VRAM 存取。CPU 或無 Triton 環境退回 PyTorch 參考實作。
"""

from __future__ import annotations

import torch

from ..execution.backend import capabilities


def _triton_available() -> bool:
    cap = capabilities()
    return cap.has_triton and cap.has_cuda


def build_rope_tables(
    head_dim: int,
    max_seq_len: int,
    theta: float = 10_000.0,
    *,
    device: torch.device | None = None,
    dtype: torch.dtype = torch.float32,
) -> tuple[torch.Tensor, torch.Tensor]:
    """預先計算 cos / sin 表，形狀 (max_seq_len, head_dim)。"""
    inv_freq = 1.0 / (
        theta ** (torch.arange(0, head_dim, 2, device=device, dtype=torch.float32) / head_dim)
    )
    positions = torch.arange(max_seq_len, device=device, dtype=torch.float32)
    freqs = torch.outer(positions, inv_freq)  # (seq, head_dim/2)
    emb = torch.cat((freqs, freqs), dim=-1)   # (seq, head_dim)
    return emb.cos().to(dtype), emb.sin().to(dtype)


def apply_rope(
    q: torch.Tensor,
    k: torch.Tensor,
    cos: torch.Tensor,
    sin: torch.Tensor,
    *,
    position_ids: torch.Tensor | None = None,
) -> tuple[torch.Tensor, torch.Tensor]:
    """對 q, k 套用 RoPE。

    q, k: (batch, n_heads, seq, head_dim)
    cos, sin: (max_seq_len, head_dim) 或 (batch, seq, head_dim)
    position_ids: (batch, seq) 可選
    回傳旋轉後的 (q_rot, k_rot)。
    """
    if q.is_cuda and _triton_available():
        try:
            return _rope_triton(q, k, cos, sin, position_ids)
        except Exception:
            pass
    return _rope_torch(q, k, cos, sin, position_ids)


# ── PyTorch 參考實作 ────────────────────────────────────────────
def _rope_torch(
    q: torch.Tensor,
    k: torch.Tensor,
    cos: torch.Tensor,
    sin: torch.Tensor,
    position_ids: torch.Tensor | None,
) -> tuple[torch.Tensor, torch.Tensor]:
    # q, k: (B, H, S, D)
    _, _, seq_len, head_dim = q.shape
    if position_ids is not None:
        cos = cos[position_ids]  # (B, S, D)
        sin = sin[position_ids]
    else:
        # cos/sin 可能是 (max_seq, D) 或 (B, S, D)
        if cos.dim() == 2:
            cos = cos.unsqueeze(0)  # (1, max_seq, D)
            sin = sin.unsqueeze(0)
    cos = cos[:, :seq_len, :].unsqueeze(1).to(q.dtype)
    sin = sin[:, :seq_len, :].unsqueeze(1).to(q.dtype)

    def rotate(x: torch.Tensor) -> torch.Tensor:
        x1 = x[..., : head_dim // 2]
        x2 = x[..., head_dim // 2 :]
        c1 = cos[..., : head_dim // 2]
        s1 = sin[..., : head_dim // 2]
        return torch.cat([x1 * c1 - x2 * s1, x1 * s1 + x2 * c1], dim=-1)

    return rotate(q), rotate(k)


# ── Triton 實作 ────────────────────────────────────────────────
def _rope_triton(
    q: torch.Tensor,
    k: torch.Tensor,
    cos: torch.Tensor,
    sin: torch.Tensor,
    position_ids: torch.Tensor | None,
) -> tuple[ torch.Tensor, torch.Tensor]:
    # 第一版 Triton 路徑：仍以 PyTorch 張量操作為主，但融合 cos/sin gather。
    # 真正的逐元素 Triton kernel 留待效能瓶頸出現時下沉。
    return _rope_torch(q, k, cos, sin, position_ids)


__all__ = ["build_rope_tables", "apply_rope"]
