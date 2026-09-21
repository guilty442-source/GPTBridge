"""INT4 真 4-bit packing：pack/unpack roundtrip、QuantizedLinear 記憶體減半。"""
from __future__ import annotations

import _xingcheng_test_support  # noqa: F401

import pytest
import torch
import torch.nn as nn

from xingcheng.infrastructure.native_transformer.kernels import (
    pack_int4,
    unpack_int4,
    quantize_per_tensor,
)
from xingcheng.infrastructure.native_transformer.quantization import (
    QuantizedLinear,
    quantize_model,
)


def test_pack_unpack_roundtrip_even() -> None:
    q = torch.randint(-8, 8, (4, 8), dtype=torch.int8)
    packed = pack_int4(q)
    assert packed.dtype == torch.uint8
    assert packed.shape == (4, 4)  # 每 byte 兩個值
    restored = unpack_int4(packed, last_dim_size=8)
    assert torch.equal(restored, q)


def test_pack_unpack_roundtrip_odd_and_extremes() -> None:
    q = torch.tensor([[-8, 7, -1, 0, 7]], dtype=torch.int8)
    packed = pack_int4(q)
    assert packed.shape == (1, 3)  # 5 值 → 3 bytes（補一個）
    restored = unpack_int4(packed, last_dim_size=5)
    assert torch.equal(restored, q)


def test_pack_rejects_wrong_dtype_and_empty() -> None:
    with pytest.raises(TypeError):
        pack_int4(torch.zeros(4, dtype=torch.float32))
    with pytest.raises(ValueError):
        pack_int4(torch.zeros(0, dtype=torch.int8))
    with pytest.raises(TypeError):
        unpack_int4(torch.zeros(4, dtype=torch.int8), last_dim_size=8)


def test_quantized_linear_int4_halves_weight_buffer() -> None:
    torch.manual_seed(0)
    linear = nn.Linear(64, 32)
    q8 = QuantizedLinear.from_linear(linear, n_bits=8)
    q4 = QuantizedLinear.from_linear(linear, n_bits=4)
    assert q4.weight_q.dtype == torch.uint8
    assert q4.weight_q.shape == (32, 32)  # 64 → 32 bytes/列
    assert q8.weight_q.numel() == 2 * q4.weight_q.numel()

    x = torch.randn(2, 64)
    ref = linear(x)
    out4 = q4(x)
    # 4-bit 量化誤差內輸出仍接近原 linear
    assert out4.shape == ref.shape
    assert (out4 - ref).abs().max() < ref.abs().max() * 0.15


def test_quantize_model_int4_smoke() -> None:
    torch.manual_seed(0)
    model = nn.Sequential(nn.Linear(32, 32), nn.Linear(32, 16))
    quantize_model(model, n_bits=4)
    for layer in model:
        assert isinstance(layer, QuantizedLinear)
        assert layer.n_bits == 4
        assert layer.weight_q.dtype == torch.uint8
    out = model(torch.randn(1, 32))
    assert out.shape == (1, 16)
    assert torch.isfinite(out).all()


def test_quantize_dequantize_value_range_int4() -> None:
    torch.manual_seed(0)
    x = torch.randn(8, 6)
    q, scale = quantize_per_tensor(x, n_bits=4)
    assert q.min() >= -8 and q.max() <= 7
    restored = unpack_int4(pack_int4(q), last_dim_size=6)
    assert torch.equal(restored, q)
