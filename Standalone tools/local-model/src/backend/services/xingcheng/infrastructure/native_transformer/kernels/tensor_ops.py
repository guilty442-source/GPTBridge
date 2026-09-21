"""星澄自研 Tensor Operations 層。

對應正式技術棧「Tensor Operations（GEMM / Softmax / Reduction /
Activation / Gather / Scatter …）」。每個運算提供：

  1. Triton kernel（CUDA 首選）
  2. PyTorch 參考實作（CPU / 無 Triton 環境的 fallback）

介面層集中管理，讓模組不直接散落呼叫 PyTorch 原語；
Triton 不可用時自動退回 PyTorch，純 CPU 環境仍可完全本地執行。
"""

from __future__ import annotations

import torch
import torch.nn.functional as F

from ..execution.backend import capabilities


def _triton_available() -> bool:
    cap = capabilities()
    return cap.has_triton and cap.has_cuda


# ── GEMM ────────────────────────────────────────────────────────

def gemm(
    a: torch.Tensor,
    b: torch.Tensor,
    *,
    bias: torch.Tensor | None = None,
    transpose_b: bool = False,
) -> torch.Tensor:
    """矩陣乘 ``a @ b``（``transpose_b=True`` 時為 ``a @ b.T``）。

    2D 輸入走 tiled Triton kernel；其餘形狀或無 Triton 時退回
    ``torch.matmul``（內部依裝置調度 cuBLASLt / oneDNN）。
    """
    if transpose_b:
        b = b.transpose(-1, -2)
    if (
        a.dim() == 2
        and b.dim() == 2
        and a.is_cuda
        and _triton_available()
    ):
        try:
            return _gemm_triton(a, b, bias)
        except Exception:
            pass
    out = torch.matmul(a, b)
    if bias is not None:
        out = out + bias
    return out


def _gemm_triton(
    a: torch.Tensor,
    b: torch.Tensor,
    bias: torch.Tensor | None,
) -> torch.Tensor:
    import triton
    import triton.language as tl

    @triton.jit
    def _gemm_kernel(
        a_ptr, b_ptr, bias_ptr, out_ptr,
        M, N, K,
        stride_am, stride_ak, stride_bk, stride_bn, stride_om, stride_on,
        HAS_BIAS: tl.constexpr,
        BM: tl.constexpr, BN: tl.constexpr, BK: tl.constexpr,
    ):
        pid_m = tl.program_id(0)
        pid_n = tl.program_id(1)
        rm = pid_m * BM + tl.arange(0, BM)
        rn = pid_n * BN + tl.arange(0, BN)
        acc = tl.zeros((BM, BN), dtype=tl.float32)
        for k0 in range(0, K, BK):
            rk = k0 + tl.arange(0, BK)
            a_tile = tl.load(
                a_ptr + rm[:, None] * stride_am + rk[None, :] * stride_ak,
                mask=(rm[:, None] < M) & (rk[None, :] < K),
                other=0.0,
            )
            b_tile = tl.load(
                b_ptr + rk[:, None] * stride_bk + rn[None, :] * stride_bn,
                mask=(rk[:, None] < K) & (rn[None, :] < N),
                other=0.0,
            )
            acc += tl.dot(a_tile, b_tile)
        if HAS_BIAS:
            acc += tl.load(bias_ptr + rn, mask=rn < N, other=0.0).to(tl.float32)[None, :]
        tl.store(
            out_ptr + rm[:, None] * stride_om + rn[None, :] * stride_on,
            acc.to(out_ptr.dtype.element_ty),
            mask=(rm[:, None] < M) & (rn[None, :] < N),
        )

    M, K = a.shape
    K2, N = b.shape
    if K != K2:
        raise ValueError(f"gemm 內維不一致: {K} vs {K2}")
    a_c = a.contiguous()
    b_c = b.contiguous()
    out = torch.empty((M, N), dtype=a.dtype, device=a.device)
    BM, BN, BK = 32, 32, 32
    grid = (triton.cdiv(M, BM), triton.cdiv(N, BN))
    _gemm_kernel[grid](
        a_c, b_c,
        bias.contiguous() if bias is not None else a_c,
        out,
        M, N, K,
        a_c.stride(0), a_c.stride(1), b_c.stride(0), b_c.stride(1),
        out.stride(0), out.stride(1),
        HAS_BIAS=bias is not None,
        BM=BM, BN=BN, BK=BK,
    )
    return out


# ── Softmax ─────────────────────────────────────────────────────

def softmax(x: torch.Tensor, dim: int = -1) -> torch.Tensor:
    """列向 softmax。最後一維 + CUDA + Triton 時走 fused kernel。"""
    if (
        x.is_cuda
        and _triton_available()
        and (dim == -1 or dim == x.dim() - 1)
        and x.is_contiguous()
    ):
        try:
            return _softmax_triton(x)
        except Exception:
            pass
    return F.softmax(x, dim=dim)


def _softmax_triton(x: torch.Tensor) -> torch.Tensor:
    import triton
    import triton.language as tl

    @triton.jit
    def _softmax_kernel(x_ptr, out_ptr, n_cols, BLOCK: tl.constexpr):
        row = tl.program_id(0)
        offs = tl.arange(0, BLOCK)
        mask = offs < n_cols
        xv = tl.load(x_ptr + row * n_cols + offs, mask=mask, other=-float("inf"))
        xv = xv.to(tl.float32)
        m = tl.max(xv, axis=0)
        e = tl.exp(xv - m)
        s = tl.sum(e, axis=0)
        tl.store(out_ptr + row * n_cols + offs, e / s, mask=mask)

    rows = x.numel() // x.shape[-1]
    n_cols = x.shape[-1]
    block = triton.next_power_of_2(n_cols)
    out = torch.empty_like(x)
    _softmax_kernel[(rows,)](x, out, n_cols, BLOCK=block)
    return out


# ── Activation ──────────────────────────────────────────────────

_ACTIVATIONS = ("silu", "gelu", "relu")


def activation(x: torch.Tensor, kind: str = "silu") -> torch.Tensor:
    """逐元素激活函式：``silu`` / ``gelu`` / ``relu``。"""
    if kind not in _ACTIVATIONS:
        raise ValueError(f"未知激活函式: {kind}（可用 {_ACTIVATIONS}）")
    if x.is_cuda and _triton_available() and x.is_contiguous():
        try:
            return _activation_triton(x, kind)
        except Exception:
            pass
    if kind == "silu":
        return F.silu(x)
    if kind == "gelu":
        return F.gelu(x)
    return F.relu(x)


def _activation_triton(x: torch.Tensor, kind: str) -> torch.Tensor:
    import triton
    import triton.language as tl

    @triton.jit
    def _act_kernel(x_ptr, out_ptr, n, KIND: tl.constexpr, BLOCK: tl.constexpr):
        pid = tl.program_id(0)
        offs = pid * BLOCK + tl.arange(0, BLOCK)
        mask = offs < n
        xv = tl.load(x_ptr + offs, mask=mask, other=0.0).to(tl.float32)
        if KIND == 0:  # silu: x * sigmoid(x)
            out = xv * tl.sigmoid(xv)
        elif KIND == 1:  # gelu tanh 近似
            inner = 0.7978845608028654 * (xv + 0.044715 * xv * xv * xv)
            out = 0.5 * xv * (1.0 + tl.math.tanh(inner))
        else:  # relu
            out = tl.maximum(xv, 0.0)
        tl.store(out_ptr + offs, out.to(out_ptr.dtype.element_ty), mask=mask)

    kind_id = _ACTIVATIONS.index(kind)
    out = torch.empty_like(x)
    n = x.numel()
    BLOCK = 1024
    _act_kernel[(triton.cdiv(n, BLOCK),)](x, out, n, KIND=kind_id, BLOCK=BLOCK)
    return out


# ── Reduction ───────────────────────────────────────────────────

def reduce_sum(x: torch.Tensor, dim: int | None = None) -> torch.Tensor:
    """加總約減（PyTorch 參考路徑；Triton 版本待實際瓶頸時下沉）。"""
    return x.sum() if dim is None else x.sum(dim=dim)


def reduce_mean(x: torch.Tensor, dim: int | None = None) -> torch.Tensor:
    return x.mean() if dim is None else x.mean(dim=dim)


def reduce_max(x: torch.Tensor, dim: int | None = None) -> torch.Tensor:
    if dim is None:
        return x.max()
    return x.max(dim=dim).values


# ── Gather / Scatter ────────────────────────────────────────────

def gather(x: torch.Tensor, dim: int, index: torch.Tensor) -> torch.Tensor:
    """沿 ``dim`` 依 ``index`` 取值（torch.gather 介面）。"""
    return torch.gather(x, dim, index)


def scatter_add(
    x: torch.Tensor, dim: int, index: torch.Tensor, src: torch.Tensor
) -> torch.Tensor:
    """沿 ``dim`` 把 ``src`` 依 ``index`` 加進 ``x``（原位複製後操作）。"""
    out = x.clone()
    return out.scatter_add_(dim, index, src)


__all__ = [
    "activation",
    "gather",
    "gemm",
    "reduce_max",
    "reduce_mean",
    "reduce_sum",
    "scatter_add",
    "softmax",
]
