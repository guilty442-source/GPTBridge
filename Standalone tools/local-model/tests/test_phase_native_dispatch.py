"""Native Dispatch: C++ core parity with Python fallback for inference."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401

import os

import pytest
import torch

from xingcheng.infrastructure.native_transformer.config import XingChengConfig
from xingcheng.infrastructure.native_transformer.execution import dispatch
from xingcheng.infrastructure.native_transformer.kernels.rmsnorm import rms_norm_weight
from xingcheng.infrastructure.native_transformer.kernels.rope import (
    _rope_torch,
    apply_rope,
    build_rope_tables,
)
from xingcheng.infrastructure.native_transformer.modules.model import (
    XingChengForCausalLM,
)


def _config() -> XingChengConfig:
    cfg = XingChengConfig.small()
    cfg.vocab_size = 264
    cfg.max_position_embeddings = 64
    return cfg


def test_dispatch_flag_defaults_on_and_env_disables(monkeypatch) -> None:
    monkeypatch.delenv(dispatch.DISPATCH_ENV, raising=False)
    assert dispatch.dispatch_enabled() is True
    monkeypatch.setenv(dispatch.DISPATCH_ENV, "0")
    assert dispatch.dispatch_enabled() is False
    assert dispatch.should_dispatch("transformer.attention", 64) is False


def test_summary_reports_backend() -> None:
    info = dispatch.summary()
    assert info["backend"] in {"native-compute-core", "python-pytorch"}
    assert isinstance(info["thresholds"], dict)
    assert "transformer.attention" in info["thresholds"]


def test_native_attention_skips_when_masked_or_grad() -> None:
    q = torch.randn(1, 2, 8, 16)
    k = torch.randn(1, 2, 8, 16)
    v = torch.randn(1, 2, 8, 16)
    with torch.no_grad():
        assert (
            dispatch.native_attention(
                q, k, v, scale=16 ** -0.5, is_causal=True,
                attention_mask=torch.ones(1, 8),
            )
            is None
        )
    grad_q = q.clone().requires_grad_(True)
    assert (
        dispatch.native_attention(
            grad_q, k, v, scale=16 ** -0.5, is_causal=True, attention_mask=None
        )
        is None
    )


def test_native_attention_returns_tensor_when_available() -> None:
    if not dispatch.native_available():
        pytest.skip("native core not built")
    q = torch.randn(1, 2, 8, 16)
    k = torch.randn(1, 2, 8, 16)
    v = torch.randn(1, 2, 8, 16)
    with torch.no_grad():
        out = dispatch.native_attention(
            q, k, v, scale=16 ** -0.5, is_causal=True, attention_mask=None
        )
    assert out is not None
    assert out.shape == q.shape


def test_native_rmsnorm_matches_reference_and_records_shape() -> None:
    if not dispatch.native_available():
        pytest.skip("native core not built")
    dispatch._VERIFIED_NORM_SHAPES.clear()
    hidden = torch.randn(2, 3, 8)
    weight = torch.randn(8)
    with torch.no_grad():
        out = dispatch.native_rmsnorm(hidden, weight, 1e-5)
    assert out is not None
    expected = hidden * torch.rsqrt(hidden.pow(2).mean(-1, keepdim=True) + 1e-5) * weight
    assert torch.allclose(out, expected, atol=1e-6, rtol=1e-6)
    assert ("rmsnorm", 6, 8) in dispatch.verified_rmsnorm_shapes()


def test_native_rmsnorm_skips_grad() -> None:
    hidden = torch.randn(2, 8, requires_grad=True)
    weight = torch.ones(8)
    assert dispatch.native_rmsnorm(hidden, weight, 1e-5) is None


def test_rmsnorm_kernel_uses_native_on_cpu() -> None:
    if not dispatch.native_available():
        pytest.skip("native core not built")
    dispatch._VERIFIED_NORM_SHAPES.clear()
    hidden = torch.randn(2, 3, 8)
    weight = torch.randn(8)
    with torch.no_grad():
        out = rms_norm_weight(hidden, weight, 1e-5)
    assert out.shape == hidden.shape
    assert ("rmsnorm", 6, 8) in dispatch.verified_rmsnorm_shapes()


def test_native_rope_matches_reference_and_records_shape() -> None:
    if not dispatch.native_available():
        pytest.skip("native core not built")
    dispatch._VERIFIED_ROPE_SHAPES.clear()
    q = torch.randn(1, 2, 64, 8)
    k = torch.randn(1, 1, 64, 8)
    cos, sin = build_rope_tables(8, 64, dtype=torch.float64)
    with torch.no_grad():
        out = dispatch.native_rope(q, k, cos, sin)
    assert out is not None
    expected_q, expected_k = _rope_torch(q, k, cos, sin, None)
    assert torch.allclose(out[0], expected_q, atol=1e-6, rtol=1e-6)
    assert torch.allclose(out[1], expected_k, atol=1e-6, rtol=1e-6)
    assert ("rope", 1, 2, 1, 64, 8, False) in dispatch.verified_rope_shapes()


def test_native_rope_skips_grad() -> None:
    q = torch.randn(1, 2, 4, 8, requires_grad=True)
    k = torch.randn(1, 2, 4, 8)
    cos, sin = build_rope_tables(8, 4)
    assert dispatch.native_rope(q, k, cos, sin) is None


def test_rope_kernel_uses_native_on_cpu() -> None:
    if not dispatch.native_available():
        pytest.skip("native core not built")
    dispatch._VERIFIED_ROPE_SHAPES.clear()
    q = torch.randn(1, 2, 64, 8)
    k = torch.randn(1, 1, 64, 8)
    cos, sin = build_rope_tables(8, 64)
    with torch.no_grad():
        q_out, k_out = apply_rope(q, k, cos, sin)
    assert q_out.shape == q.shape
    assert k_out.shape == k.shape
    assert ("rope", 1, 2, 1, 64, 8, False) in dispatch.verified_rope_shapes()


def test_dispatch_matches_python_fallback_logits(monkeypatch) -> None:
    if not dispatch.native_available():
        pytest.skip("native core not built")
    cfg = _config()
    torch.manual_seed(5)
    model = XingChengForCausalLM(cfg)
    model.eval()
    ids = torch.randint(4, 260, (2, 16))

    monkeypatch.setenv(dispatch.DISPATCH_ENV, "1")
    with torch.no_grad():
        native_logits = model(ids)["logits"].clone()

    monkeypatch.setenv(dispatch.DISPATCH_ENV, "0")
    with torch.no_grad():
        python_logits = model(ids)["logits"].clone()

    assert torch.allclose(native_logits, python_logits, atol=1e-4, rtol=1e-4)


def test_training_path_keeps_autograd(monkeypatch) -> None:
    """訓練路徑不得被派送攔截：梯度必須存在。"""
    cfg = _config()
    torch.manual_seed(7)
    model = XingChengForCausalLM(cfg)
    monkeypatch.setenv(dispatch.DISPATCH_ENV, "1")
    ids = torch.randint(4, 260, (2, 8))
    out = model(ids, labels=ids)
    out["loss"].backward()
    grads = [p.grad for p in model.parameters() if p.grad is not None]
    assert grads
    assert all(torch.isfinite(grad).all() for grad in grads)
