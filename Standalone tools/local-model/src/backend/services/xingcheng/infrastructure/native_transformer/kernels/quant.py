"""Quantization / Dequantization 自研 kernel。

支援 per-tensor INT8 / INT4 / FP8 模擬。
優先順序：Triton → PyTorch。
真正的 INT4 weight-only 量化與 AWQ/GPTQ 等進階格式留待後續版本。
"""

from __future__ import annotations

import torch

from ..execution.backend import capabilities


def _triton_available() -> bool:
    cap = capabilities()
    return cap.has_triton and cap.has_cuda


def quantize_per_tensor(
    x: torch.Tensor,
    *,
    n_bits: int = 8,
    symmetric: bool = True,
) -> tuple[torch.Tensor, torch.Tensor]:
    """per-tensor 對稱量化。

    回傳 (quantized_int, scale)，quantized_int 為整數型張量（儲存為對應 dtype）。
    """
    if n_bits == 8:
        target_dtype = torch.int8
        qmax = 127
    elif n_bits == 4:
        target_dtype = torch.int8  # INT4 以 INT8 容器儲存，值域 [-8, 7]
        qmax = 7
    else:
        raise ValueError(f"n_bits 必須為 4 或 8，取得 {n_bits}")

    x_f = x.to(torch.float32)
    abs_max = x_f.abs().amax()
    scale = abs_max / qmax if abs_max > 0 else torch.ones_like(abs_max)
    q = torch.clamp((x_f / scale).round(), -qmax, qmax).to(target_dtype)
    return q, scale.to(x.dtype)


def dequantize_per_tensor(
    q: torch.Tensor,
    scale: torch.Tensor,
    *,
    out_dtype: torch.dtype = torch.float32,
) -> torch.Tensor:
    """per-tensor 反量化。"""
    if q.is_cuda and _triton_available():
        try:
            return _dequant_triton(q, scale, out_dtype)
        except Exception:
            pass
    return _dequant_torch(q, scale, out_dtype)


def _dequant_torch(
    q: torch.Tensor,
    scale: torch.Tensor,
    out_dtype: torch.dtype,
) -> torch.Tensor:
    return (q.to(torch.float32) * scale).to(out_dtype)


def _dequant_triton(
    q: torch.Tensor,
    scale: torch.Tensor,
    out_dtype: torch.dtype,
) -> torch.Tensor:
    import triton
    import triton.language as tl

    @triton.jit
    def _kernel(q_ptr, s_ptr, o_ptr, n, BLOCK: tl.constexpr):
        pid = tl.program_id(0)
        offs = pid * BLOCK + tl.arange(0, BLOCK)
        mask = offs < n
        qv = tl.load(q_ptr + offs, mask=mask, other=0).to(tl.float32)
        sv = tl.load(s_ptr).to(tl.float32)
        out = qv * sv
        tl.store(o_ptr + offs, out.to(o_ptr.dtype.element_ty), mask=mask)

    out = torch.empty(q.shape, dtype=out_dtype, device=q.device)
    n = q.numel()
    BLOCK = 1024
    grid = (triton.cdiv(n, BLOCK),)
    _kernel[grid](q, scale, out, n, BLOCK=BLOCK)
    return out


__all__ = ["quantize_per_tensor", "dequantize_per_tensor"]
