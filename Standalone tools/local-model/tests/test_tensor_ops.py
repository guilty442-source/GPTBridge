"""kernels/tensor_ops：GEMM / softmax / activation / gather / scatter /
reduction 的 PyTorch fallback 路徑正確性（本機無 Triton，GPU 路徑
由 dispatch 結構自動退回）。"""
from __future__ import annotations

import _xingcheng_test_support  # noqa: F401

import pytest
import torch
import torch.nn.functional as F

from xingcheng.infrastructure.native_transformer.kernels import (
    activation,
    gather,
    gemm,
    reduce_max,
    reduce_mean,
    reduce_sum,
    scatter_add,
    softmax,
)


def test_gemm_matches_matmul_2d() -> None:
    torch.manual_seed(0)
    a = torch.randn(8, 16)
    b = torch.randn(16, 4)
    assert torch.allclose(gemm(a, b), a @ b)


def test_gemm_bias_and_transpose_b() -> None:
    torch.manual_seed(0)
    a = torch.randn(4, 8)
    b = torch.randn(6, 8)  # transpose_b 情境：b 為 (N, K)
    bias = torch.randn(6)
    out = gemm(a, b, bias=bias, transpose_b=True)
    assert out.shape == (4, 6)
    assert torch.allclose(out, a @ b.T + bias)


def test_gemm_batched() -> None:
    torch.manual_seed(0)
    a = torch.randn(2, 4, 8)
    b = torch.randn(2, 8, 4)
    assert torch.allclose(gemm(a, b), torch.matmul(a, b))


def test_gemm_inner_dim_mismatch() -> None:
    with pytest.raises((ValueError, RuntimeError)):
        gemm(torch.randn(2, 4), torch.randn(5, 3))


def test_softmax_rows_sum_to_one() -> None:
    x = torch.randn(4, 32)
    out = softmax(x)
    assert torch.allclose(out, F.softmax(x, dim=-1))
    assert torch.allclose(out.sum(dim=-1), torch.ones(4))


def test_softmax_inner_dim() -> None:
    x = torch.randn(4, 8)
    out = softmax(x, dim=0)
    assert torch.allclose(out, F.softmax(x, dim=0))


def test_activation_variants() -> None:
    x = torch.randn(16)
    assert torch.allclose(activation(x, "silu"), F.silu(x))
    assert torch.allclose(activation(x, "relu"), F.relu(x))
    assert torch.allclose(activation(x, "gelu"), F.gelu(x), atol=1e-3)
    with pytest.raises(ValueError):
        activation(x, "swish-v9")


def test_reductions() -> None:
    x = torch.randn(4, 8)
    assert torch.isclose(reduce_sum(x), x.sum())
    assert torch.isclose(reduce_mean(x, dim=1)[0], x[0].mean())
    assert torch.isclose(reduce_max(x, dim=0)[0], x[:, 0].max())


def test_gather_scatter() -> None:
    x = torch.arange(12, dtype=torch.float32).reshape(3, 4)
    idx = torch.tensor([[0], [2], [1]])
    assert torch.equal(gather(x, 1, idx), torch.tensor([[0.0], [6.0], [9.0]]))

    base = torch.zeros(2, 4)
    src = torch.ones(2, 4)
    index = torch.tensor([[0, 1, 0, 1], [1, 0, 1, 0]])
    out = scatter_add(base, 1, index, src)
    # index 只覆蓋位置 0,1 各兩次；位置 2,3 保持 0
    assert torch.equal(out, torch.tensor([[2.0, 2.0, 0.0, 0.0]] * 2))
    assert torch.equal(base, torch.zeros(2, 4))  # 原張量不被修改
