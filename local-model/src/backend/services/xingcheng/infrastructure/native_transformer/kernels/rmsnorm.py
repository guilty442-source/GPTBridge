"""RMSNorm 自研 kernel。

優先順序：Triton → PyTorch。
Triton 路徑在 CUDA 上提供 operator fusion（norm + scale + eps），
降低 VRAM 存取量。CPU 或無 Triton 環境退回 PyTorch 參考實作。
"""

from __future__ import annotations

import torch

from ..execution.backend import capabilities


def _triton_available() -> bool:
    cap = capabilities()
    return cap.has_triton and cap.has_cuda


def rms_norm_weight(
    hidden_states: torch.Tensor,
    weight: torch.Tensor,
    eps: float,
) -> torch.Tensor:
    """RMSNorm：x / sqrt(mean(x^2) + eps) * weight。

    自動選擇 Triton 或 PyTorch 實作。
    """
    if hidden_states.is_cuda and _triton_available():
        try:
            return _rms_norm_triton(hidden_states, weight, eps)
        except Exception:
            # 任何 ABI / kernel 編譯問題都退回 PyTorch
            pass
    return _rms_norm_torch(hidden_states, weight, eps)


# 別名供模組對外使用
rms_norm = rms_norm_weight


# ── PyTorch 參考實作 ────────────────────────────────────────────
def _rms_norm_torch(
    hidden_states: torch.Tensor,
    weight: torch.Tensor,
    eps: float,
) -> torch.Tensor:
    input_dtype = hidden_states.dtype
    hidden_states = hidden_states.to(torch.float32)
    variance = hidden_states.pow(2).mean(-1, keepdim=True)
    hidden_states = hidden_states * torch.rsqrt(variance + eps)
    return (weight * hidden_states.to(input_dtype)).to(input_dtype)


# ── Triton 實作 ────────────────────────────────────────────────
def _rms_norm_triton(
    hidden_states: torch.Tensor,
    weight: torch.Tensor,
    eps: float,
) -> torch.Tensor:
    import triton
    import triton.language as tl

    @triton.jit
    def _kernel(
        x_ptr,
        w_ptr,
        out_ptr,
        n_cols,
        eps,
        BLOCK_SIZE: tl.constexpr,
    ):
        row = tl.program_id(0)
        cols = tl.arange(0, BLOCK_SIZE)
        mask = cols < n_cols
        x = tl.load(x_ptr + row * n_cols + cols, mask=mask, other=0.0).to(tl.float32)
        variance = tl.sum(x * x, axis=0) / n_cols
        normed = x * tl.rsqrt(variance + eps)
        w = tl.load(w_ptr + cols, mask=mask, other=0.0).to(tl.float32)
        out = normed * w
        tl.store(out_ptr + row * n_cols + cols, out.to(x_ptr.dtype.element_ty), mask=mask)

    *batch, n_cols = hidden_states.shape
    out = torch.empty_like(hidden_states)
    x_2d = hidden_states.reshape(-1, n_cols)
    out_2d = out.reshape(-1, n_cols)
    BLOCK_SIZE = triton.next_power_of_2(n_cols)
    _kernel[(x_2d.shape[0],)](
        x_2d,
        weight,
        out_2d,
        n_cols,
        eps,
        BLOCK_SIZE=BLOCK_SIZE,
    )
    return out


__all__ = ["rms_norm", "rms_norm_weight"]
