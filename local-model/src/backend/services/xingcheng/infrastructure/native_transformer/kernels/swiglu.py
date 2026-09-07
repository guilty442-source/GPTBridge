"""SwiGLU 自研 kernel。

SwiGLU(x) = (silu(W1 x) * W3 x) W2
本模組只負責前段的 SiLU + element-wise multiply（即 gate 部分），
後段 W2 投影由 nn.Linear 處理（走 cuBLASLt / oneDNN）。

優先順序：Triton → PyTorch。
"""

from __future__ import annotations

import torch
import torch.nn.functional as F

from ..execution.backend import capabilities


def _triton_available() -> bool:
    cap = capabilities()
    return cap.has_triton and cap.has_cuda


def swiglu(gate: torch.Tensor, up: torch.Tensor) -> torch.Tensor:
    """SiLU(gate) * up。gate, up 同形狀。"""
    if gate.is_cuda and _triton_available():
        try:
            return _swiglu_triton(gate, up)
        except Exception:
            pass
    return _swiglu_torch(gate, up)


def _swiglu_torch(gate: torch.Tensor, up: torch.Tensor) -> torch.Tensor:
    return F.silu(gate) * up


def _swiglu_triton(gate: torch.Tensor, up: torch.Tensor) -> torch.Tensor:
    import triton
    import triton.language as tl

    @triton.jit
    def _kernel(g_ptr, u_ptr, o_ptr, n, BLOCK: tl.constexpr):
        pid = tl.program_id(0)
        offs = pid * BLOCK + tl.arange(0, BLOCK)
        mask = offs < n
        g = tl.load(g_ptr + offs, mask=mask, other=0.0).to(tl.float32)
        u = tl.load(u_ptr + offs, mask=mask, other=0.0).to(tl.float32)
        silu = g / (1.0 + tl.exp(-g))
        out = silu * u
        tl.store(o_ptr + offs, out.to(g_ptr.dtype.element_ty), mask=mask)

    out = torch.empty_like(gate)
    n = gate.numel()
    BLOCK = 1024
    grid = (triton.cdiv(n, BLOCK),)
    _kernel[grid](gate, up, out, n, BLOCK=BLOCK)
    return out


__all__ = ["swiglu"]
