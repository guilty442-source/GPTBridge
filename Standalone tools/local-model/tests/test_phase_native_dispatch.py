"""Native Dispatch: C++ core parity with Python fallback for inference."""
from __future__ import annotations

import _xingcheng_test_support as _support  # noqa: F401

import os

import pytest
import torch

from xingcheng.infrastructure.native_transformer.config import XingChengConfig
from xingcheng.infrastructure.native_transformer.execution import dispatch
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
    assert info["backend"] in {"c++-native-core", "python-pytorch"}
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
