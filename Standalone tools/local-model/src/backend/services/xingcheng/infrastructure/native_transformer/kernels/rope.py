"""Rotary Position Embedding (RoPE) 自研 kernel。

優先順序：Triton → 原生 C → PyTorch。
RoPE 為 Attention 的 Q/K 旋轉位置編碼；Triton 路徑融合 cos/sin 計算與旋轉，
降低 VRAM 存取。CPU 優先派送原生 C 核心，失敗再退回 PyTorch 參考實作。
"""

from __future__ import annotations

import torch

from ..execution.backend import capabilities
from ..execution.dispatch import native_rope


def _triton_available() -> bool:
    from ..execution.backend import triton_kernels_enabled

    cap = capabilities()
    return bool(cap.has_triton and cap.has_cuda and triton_kernels_enabled())


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
    # RoPE 會作用在需要梯度的 q/k 上；Triton 路徑不帶 autograd，
    # 訓練時必須退回 PyTorch 實作以保留梯度。
    if q.is_cuda and _triton_available() and not (q.requires_grad or k.requires_grad):
        try:
            return _rope_triton(q, k, cos, sin, position_ids)
        except Exception:
            pass
    native = native_rope(q, k, cos, sin, position_ids)
    if native is not None:
        return native
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
def _gather_cos_sin(
    q: torch.Tensor,
    cos: torch.Tensor,
    sin: torch.Tensor,
    position_ids: torch.Tensor | None,
) -> tuple[torch.Tensor, torch.Tensor]:
    """把 cos/sin 表展成與 q 對齊的 (B, S, D) fp32 連續張量。"""
    _, _, seq_len, _ = q.shape
    if position_ids is not None:
        cos = cos[position_ids]
        sin = sin[position_ids]
    elif cos.dim() == 2:
        cos = cos.unsqueeze(0).expand(q.shape[0], -1, -1)
        sin = sin.unsqueeze(0).expand(q.shape[0], -1, -1)
    cos = cos[:, :seq_len, :]
    sin = sin[:, :seq_len, :]
    return (
        cos.to(torch.float32).contiguous(),
        sin.to(torch.float32).contiguous(),
    )


def _rope_triton(
    q: torch.Tensor,
    k: torch.Tensor,
    cos: torch.Tensor,
    sin: torch.Tensor,
    position_ids: torch.Tensor | None,
) -> tuple[torch.Tensor, torch.Tensor]:
    import triton
    import triton.language as tl

    @triton.jit
    def _rope_kernel(
        x_ptr, cos_ptr, sin_ptr, out_ptr,
        H, S, D, D2, total,
        BLOCK: tl.constexpr,
    ):
        pid = tl.program_id(0)
        offs = pid * BLOCK + tl.arange(0, BLOCK)
        mask = offs < total
        # 每個元素 = 一個 (b, h, s, i) pair，i ∈ [0, D/2)
        i = offs % D2
        rest = offs // D2
        s = rest % S
        rest = rest // S
        h = rest % H
        b = rest // H
        x_off = ((b * H + h) * S + s) * D + i
        c_off = (b * S + s) * D + i
        x1 = tl.load(x_ptr + x_off, mask=mask, other=0.0).to(tl.float32)
        x2 = tl.load(x_ptr + x_off + D2, mask=mask, other=0.0).to(tl.float32)
        c1 = tl.load(cos_ptr + c_off, mask=mask, other=0.0)
        s1 = tl.load(sin_ptr + c_off, mask=mask, other=0.0)
        tl.store(
            out_ptr + x_off,
            (x1 * c1 - x2 * s1).to(out_ptr.dtype.element_ty),
            mask=mask,
        )
        tl.store(
            out_ptr + x_off + D2,
            (x1 * s1 + x2 * c1).to(out_ptr.dtype.element_ty),
            mask=mask,
        )

    cos_g, sin_g = _gather_cos_sin(q, cos, sin, position_ids)

    def _launch(x: torch.Tensor) -> torch.Tensor:
        # GQA：k 的 head 數可能少於 q，launch 參數必須逐張量計算，
        # 否則會以 q 的 head 數越界讀取 k。
        B, heads, S, D = x.shape
        D2 = D // 2
        total = B * heads * S * D2
        block = 256
        grid = (triton.cdiv(total, block),)
        out = torch.empty_like(x)
        _rope_kernel[grid](x, cos_g, sin_g, out, heads, S, D, D2, total, BLOCK=block)
        return out

    return _launch(q), _launch(k)


__all__ = ["build_rope_tables", "apply_rope"]
